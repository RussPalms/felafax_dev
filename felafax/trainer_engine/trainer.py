from dataclasses import dataclass
from typing import Optional, Any
import functools
import pyrallis
import jax
# jax.distributed.initialize()

import equinox as eqx
import quax
import jax.numpy as jnp
from jax.experimental import mesh_utils
from jax.sharding import NamedSharding, PartitionSpec as PS

import optax

from felafax.trainer_engine.checkpoint import (
    Checkpointer,
    load_model,
    save_model_to_hf,
)

import os


def get_mesh(num_tpus: int):
    mesh_shape = None
    if num_tpus == 4:
        mesh_shape = (1, 2, 2)
    elif num_tpus == 8:
        mesh_shape = (2, 2, 2)
    else:
        raise ValueError(f"Invalid number of TPUs: {num_tpus}")

    print(f"Creating TPU device mesh with shape {mesh_shape}...")
    device_mesh = mesh_utils.create_device_mesh(mesh_shape)
    mesh = jax.sharding.Mesh(
        device_mesh, axis_names=("batch", "fsdp", "mp")
    )
    return mesh


def _preprocess_batch(batch):
    # Convert PyTorch tensors to JAX arrays
    batch = {
        k: v if isinstance(v, jax.Array) else jax.numpy.array(v.numpy())
        for k, v in batch.items()
    }
    batch["input_ids"] = batch["input_ids"].astype(jnp.int32)
    batch["labels"] = batch["labels"].astype(jnp.int32)

    # Add position IDs to batch
    seq_length = batch["input_ids"].shape[1]
    batch["position_ids"] = jnp.repeat(
        jnp.arange(seq_length)[None, :],
        batch["input_ids"].shape[0],
        axis=0,
    )
    return batch


def _cross_entropy_loss_and_accuracy(logits, tokens, mask=None):
    if mask is None:
        mask = jnp.ones(tokens.shape[:2])
    mask = mask.astype(jnp.float32)

    valid_text_length = jnp.maximum(jnp.sum(mask, axis=-1), 1e-10)

    logits = logits.astype(jnp.float32)  # for numerical stability
    token_log_prob = jnp.squeeze(
        jnp.take_along_axis(
            jax.nn.log_softmax(logits, axis=-1),
            jnp.expand_dims(tokens, -1),
            axis=-1,
        ),
        -1,
    )
    token_log_prob = jnp.where(mask > 0.0, token_log_prob, jnp.array(0.0))
    loss = -jnp.mean(jnp.sum(token_log_prob, axis=-1) / valid_text_length)
    correct = jnp.where(
        mask > 0.0, jnp.argmax(logits, axis=-1) == tokens, jnp.array(False)
    )
    accuracy = jnp.mean(jnp.sum(correct, axis=-1) / valid_text_length)
    return loss, accuracy


# Define configuration flags and default values
@dataclass
class TrainerConfig:
    """Configuration for the Llama trainer"""

    # Model configuration
    model_name: str = "meta-llama/Llama-3.2-1B"
    param_dtype: str = "float32"
    output_dtype: str = "float32"

    # Training configuration
    num_epochs: int = 1
    num_steps: int = 5
    num_tpus: int = jax.device_count()
    
    # Environment configuration
    base_dir: str = "/mnt/persistent-disk"



# Core trainer class -- add non-essential things in private functions.
class Trainer:
    def __init__(
        self,
        trainer_config: TrainerConfig,
        train_dataloader: Any,
        val_dataloader: Any,
        mesh: Optional[jax.sharding.Mesh] = None,
        checkpointer: Optional[Checkpointer] = None,
    ):
        assert trainer_config.model_name, "model_name must be provided"

        self.trainer_config = trainer_config
        self.train_dataloader = train_dataloader
        self.val_dataloader = val_dataloader
        self.mesh = mesh if mesh else get_mesh(trainer_config.num_tpus)
        self.checkpointer = checkpointer

        self.model, self.model_config = load_model(
            model_name=trainer_config.model_name,
        )
        self.configure_optimizers()

    def configure_optimizers(self):
        self.optimizer = optax.sgd(learning_rate=1e-3)
        self.opt_state = self.optimizer.init(
            eqx.filter(self.model, eqx.is_array)
        )

    # TODO: Add microbatching (nando ref).
    @functools.partial(jax.jit, static_argnames=("self", "model_static"))
    def forward(self, model_params, model_static, batch):
        """Computes loss for a single forward and backward pass."""
        model = eqx.combine(model_params, model_static)

        input_ids = batch["input_ids"]
        labels = batch["labels"]
        attention_mask = batch.get("attention_mask", None)
        position_ids = batch.get("position_ids", None)

        logits = model(input_ids, attention_mask, position_ids)

        # Shift for next-token prediction
        shifted_logits = logits[..., :-1, :]  # Remove last logit
        shifted_labels = labels[..., 1:]  # Remove first token

        # If using attention mask, shift it too
        shifted_mask = None
        if attention_mask is not None:
            shifted_mask = attention_mask[..., 1:]

        loss, accuracy = _cross_entropy_loss_and_accuracy(
            shifted_logits, shifted_labels, shifted_mask
        )
        return loss, accuracy

    def training_step(
        self, model_params, model_static, optimizer, optimizer_state, batch
    ):
        grad_fn = jax.value_and_grad(self.forward, argnums=(0), has_aux=True)
        (loss, accuracy), grads = grad_fn(
            model_params,
            model_static=model_static,
            batch=batch,
        )
        updates, optimizer_state = optimizer.update(
            grads, optimizer_state, model_params
        )
        model_params = optax.apply_updates(model_params, updates)
        return loss, (accuracy, model_params, optimizer_state)

    def validation_step(
        self, *, model_params, model_static, optimizer, optimizer_state, batch
    ):
        pass

    def train(self):
        model_params, model_static = eqx.partition(self.model, eqx.is_array)
        optimizer_state = self.opt_state
        max_steps = self.trainer_config.num_steps or float("inf")

        prev_step = 0
        prev_loss = 0.0
        prev_accuracy = 0.0

        for step, batch in enumerate(self.train_dataloader):
            if step >= max_steps:
                break

            if step:
                # Printing metrics of previous step, so that XLA pipelining is not disrupted.
                print(
                    f"Step {prev_step}: Loss: {prev_loss:.4f}, Accuracy: {prev_accuracy:.4f}"
                )

            batch = _preprocess_batch(batch)
            batch = jax.device_put(batch, NamedSharding(self.mesh, PS("batch")))
            optimizer_state = jax.device_put(
                optimizer_state, NamedSharding(self.mesh, PS())
            )

            loss, (accuracy, model_params, optimizer_state) = self.training_step(
                model_params=model_params,
                model_static=model_static,
                optimizer=self.optimizer,
                optimizer_state=optimizer_state,
                batch=batch,
            )

            prev_step = step + 1
            prev_loss = loss
            prev_accuracy = accuracy

            if self.checkpointer:
                self.checkpointer.save_checkpoint(
                    model=eqx.combine(model_params, model_static),
                    model_config=self.model_config,
                    step=step + 1,
                )

        # Save final checkpoint
        if self.checkpointer:
            self.checkpointer.save_checkpoint(
                model=eqx.combine(model_params, model_static),
                model_config=self.model_config,
                step=step + 1,
            )
            self.checkpointer.wait_until_finished()
            print("Final checkpoint saved at:", self.checkpointer.checkpoint_dir)

        self.model = eqx.combine(model_params, model_static)
        print("Training completed!")

        # After training, convert and save the model in Hugging Face format
        export_dir = os.path.join(self.trainer_config.base_dir, "hf_export")
        save_model_to_hf(
            model=self.model,
            model_config=self.model_config,
            output_dir=export_dir,
            tokenizer_name=self.trainer_config.model_name,  # Use the same tokenizer as the original model
        )

        print("Hugging Face model saved at:", export_dir)

