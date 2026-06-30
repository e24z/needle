import json
import os
import time
from dataclasses import dataclass
from pathlib import Path

import mlx.core as mx
import mlx.nn as nn
import numpy as np
from mlx_lm import load

from ...runtime import naming
from .batching import (
    BatchRetryFailed,
    score_batches_with_retry,
    split_batches_by_padded_token_budget,
)
from .chunking import (
    TokenChunk,
    bucket_token_chunks,
    estimate_token_count,
    merge_token_scores_from_chunks,
    split_text_into_token_chunks,
)
from .config import (
    CHUNK_OVERLAP_ENV_NAMES,
    MAX_BATCH_SIZE_ENV_NAMES,
    MAX_LENGTH_RATIO_ENV_NAMES,
    MLX_CACHE_LIMIT_ENV_NAMES,
    MLX_CLEAR_CACHE_ENV_NAMES,
    MLX_LIGHT_ENV_NAMES,
    MLX_WIRED_LIMIT_ENV_NAMES,
    PROFILE_MLX_ENV_NAMES,
    THRESHOLD_ENV_NAMES,
    choose_mlx_max_length,
    configured_max_batch_tokens,
    configured_max_length,
    first_env,
    repair_enabled_for_active_package,
)
from .lines import aggregate_token_scores_to_lines, prune_code_lines

# The optional C++ Viterbi extension is not part of current Needle; the numpy
# decoder below is the only active path. (Historical source lives in
# ~/repos/needle if ever worth porting.)
viterbi_cpp = None

_PROMPT_PREFIX = '<|im_start|>system\nJudge whether the Document meets the requirements based on the Query and the Instruct provided. Note that the answer can only be "yes" or "no".<|im_end|>\n<|im_start|>user\n'
_PROMPT_SUFFIX = "<|im_end|>\n<|im_start|>assistant\n<think>\n\n</think>\n\n"
_MIN_CODE_TOKENS = 100
_BACKBONE_REVISION_ENV_NAMES = ("NEEDLE_BACKBONE_REVISION", "HAY_BACKBONE_REVISION")


@dataclass(frozen=True)
class _PreparedChunk:
    chunk: TokenChunk
    input_ids: list[int]
    code_offsets: list[tuple[int, int]]
    doc_start: int
    doc_end: int
    original_code_tokens: int

    @property
    def real_len(self) -> int:
        return len(self.input_ids)

    @property
    def code_tokens(self) -> int:
        return self.doc_end - self.doc_start


@dataclass(frozen=True)
class _ResolvedBackbone:
    repo: str
    requested_revision: str
    resolved_revision: str
    path: str | None = None


def _mlx_func(name: str):
    fn = getattr(mx, name, None)
    if fn is not None:
        return fn
    metal = getattr(mx, "metal", None)
    return getattr(metal, name, None) if metal is not None else None


def _env_flag(names: str | tuple[str, ...], default: bool) -> bool:
    if isinstance(names, str):
        names = (names,)
    value = first_env(names)
    if value is None:
        return default
    return value.lower() not in {"0", "false", "no", "off"}


def _env_mb(names: str | tuple[str, ...]) -> int | None:
    if isinstance(names, str):
        names = (names,)
    value = first_env(names)
    if not value:
        return None
    try:
        mb = int(value)
    except ValueError as exc:
        raise ValueError(f"{names[0]} must be an integer number of MB") from exc
    if mb < 0:
        raise ValueError(f"{names[0]} must be non-negative")
    return mb


def _requested_backbone_revision(config: dict[str, object]) -> str | None:
    revision = first_env(_BACKBONE_REVISION_ENV_NAMES)
    if revision is None:
        raw = config.get("backbone_revision") or config.get("backbone_model_revision")
        revision = str(raw) if raw is not None else None
    revision = revision.strip() if revision else None
    return revision or None


def _resolve_backbone_reference(
    backbone_name: str,
    config: dict[str, object],
) -> _ResolvedBackbone:
    local_path = Path(backbone_name).expanduser()
    if local_path.is_dir():
        return _ResolvedBackbone(
            repo=backbone_name,
            requested_revision="local",
            resolved_revision="local",
            path=str(local_path),
        )

    from needle.model_download import resolve_model_revision

    revision = _requested_backbone_revision(config)
    return _ResolvedBackbone(
        repo=backbone_name,
        requested_revision=revision or "default",
        resolved_revision=resolve_model_revision(backbone_name, revision),
    )


def _download_backbone_snapshot(backbone: _ResolvedBackbone) -> str:
    if backbone.path:
        return backbone.path

    from needle.model_download import download_model_snapshot

    result = download_model_snapshot(
        repo=backbone.repo,
        revision=backbone.resolved_revision,
        caller="runtime-backbone",
        force=False,
    )
    return result.path


def _set_mlx_limit(name: str, limit_mb: int | None) -> None:
    if limit_mb is None:
        return
    fn = _mlx_func(name)
    if fn is None:
        return
    fn(limit_mb * 1024 * 1024)


def _clear_mlx_cache() -> None:
    fn = _mlx_func("clear_cache")
    if fn is not None:
        fn()


def _reset_mlx_peak_memory() -> None:
    fn = _mlx_func("reset_peak_memory")
    if fn is not None:
        fn()


def _mlx_memory_mb() -> dict[str, float]:
    stats: dict[str, float] = {}
    for key, fn_name in (
        ("active_mb", "get_active_memory"),
        ("cache_mb", "get_cache_memory"),
        ("peak_mb", "get_peak_memory"),
    ):
        fn = _mlx_func(fn_name)
        if fn is None:
            continue
        try:
            stats[key] = float(fn()) / (1024 * 1024)
        except Exception:  # noqa: BLE001
            continue
    return stats


def _is_mlx_resource_error(exc: BaseException) -> bool:
    if isinstance(exc, MemoryError):
        return True
    message = str(exc).lower()
    return any(
        marker in message
        for marker in (
            "out of memory",
            "oom",
            "failed to allocate",
            "allocation failed",
            "resource exhausted",
            "metal resource",
            "command buffer",
        )
    )


def _force_mlx_eval(*arrays: mx.array) -> None:
    fn = getattr(mx, "eval", None)
    if fn is not None:
        fn(*arrays)


def _viterbi_decode_numpy(
    emissions: np.ndarray,
    transitions: np.ndarray,
    start_transitions: np.ndarray,
    end_transitions: np.ndarray,
    mask: np.ndarray,
) -> np.ndarray:
    """Decode CRF emissions without the optional C++ extension."""
    batch_size, seq_len, num_tags = emissions.shape
    paths = np.zeros((batch_size, seq_len), dtype=np.int64)

    for batch_idx in range(batch_size):
        active_len = int(np.asarray(mask[batch_idx]).astype(bool).sum())
        if active_len <= 0:
            continue

        score = start_transitions + emissions[batch_idx, 0]
        history = np.zeros((active_len, num_tags), dtype=np.int64)

        for step in range(1, active_len):
            next_score = (
                score[:, None] + transitions + emissions[batch_idx, step][None, :]
            )
            history[step] = np.argmax(next_score, axis=0)
            score = np.max(next_score, axis=0)

        best_tag = int(np.argmax(score + end_transitions))
        paths[batch_idx, active_len - 1] = best_tag
        for step in range(active_len - 1, 0, -1):
            best_tag = int(history[step, best_tag])
            paths[batch_idx, step - 1] = best_tag

    return paths


class MLXCRFLayer(nn.Module):
    def __init__(self, num_tags: int = 2):
        super().__init__()
        self.num_tags = num_tags
        self.transitions = mx.zeros((num_tags, num_tags))
        self.start_transitions = mx.zeros((num_tags,))
        self.end_transitions = mx.zeros((num_tags,))

    def decode(self, emissions: mx.array, mask: mx.array = None) -> mx.array:
        batch_size, seq_len, _ = emissions.shape
        if mask is None:
            mask = mx.ones((batch_size, seq_len), dtype=mx.bool_)

        # Convert to numpy arrays for the C++ decoder
        emissions_np = np.array(emissions)
        transitions_np = np.array(self.transitions)
        start_np = np.array(self.start_transitions)
        end_np = np.array(self.end_transitions)
        mask_np = np.array(mask)

        if viterbi_cpp is not None:
            best_paths_np = viterbi_cpp.decode(
                emissions_np, transitions_np, start_np, end_np, mask_np
            )
        else:
            best_paths_np = _viterbi_decode_numpy(
                emissions_np, transitions_np, start_np, end_np, mask_np
            )

        return mx.array(best_paths_np)


class MLXCRFCompressionHead(nn.Module):
    def __init__(self, input_dim: int, bottleneck: int = 256, dropout: float = 0.1):
        super().__init__()
        self.feature_extractor = nn.Sequential(
            nn.LayerNorm(input_dim),
            nn.Linear(input_dim, bottleneck),
            nn.GELU(),
            nn.Dropout(dropout),
            nn.Linear(bottleneck, 2),
        )
        self.crf = MLXCRFLayer(num_tags=2)

    def __call__(self, x: mx.array) -> mx.array:
        # Returns raw token emissions [B, L, 2]
        return self.feature_extractor(x)

    def decode(self, x: mx.array, mask: mx.array = None) -> mx.array:
        emissions = self.feature_extractor(x)
        return self.crf.decode(emissions, mask)


class MLXTokenScorer(nn.Module):
    def __init__(
        self,
        hidden_size: int,
        bottleneck: int = 256,
        dropout: float = 0.1,
        num_fusion_layers: int = 1,
        num_heads: int = 8,
        use_multi_layer_fusion: bool = True,
        early_layer_ratio: float = 0.25,
        middle_layer_ratio: float = 0.5,
        compression_head_type: str = "crf",
        num_hidden_layers: int = 24,
    ):
        super().__init__()
        self.use_multi_layer_fusion = use_multi_layer_fusion
        self.compression_head_type = compression_head_type
        self.num_hidden_layers = num_hidden_layers
        self.final_layer_idx = num_hidden_layers

        if self.use_multi_layer_fusion:
            self.early_layer_idx = max(1, int(num_hidden_layers * early_layer_ratio))
            self.middle_layer_idx = max(1, int(num_hidden_layers * middle_layer_ratio))
            self.fused_hidden_size = hidden_size * 3
        else:
            self.fused_hidden_size = hidden_size

        self.dropout = nn.Dropout(dropout)
        self.fusion_layers = [
            nn.MultiHeadAttention(self.fused_hidden_size, num_heads, bias=True)
            for _ in range(num_fusion_layers)
        ]
        self.fusion_norms = [
            nn.LayerNorm(self.fused_hidden_size) for _ in range(num_fusion_layers)
        ]

        if compression_head_type == "crf":
            self.compression_head = MLXCRFCompressionHead(
                self.fused_hidden_size, bottleneck, dropout
            )
        elif compression_head_type == "ffn":
            expansion_dim = bottleneck * 2
            self.compression_head = nn.Sequential(
                nn.LayerNorm(self.fused_hidden_size),
                nn.Linear(self.fused_hidden_size, expansion_dim),
                nn.GELU(),
                nn.Dropout(dropout),
                nn.Linear(expansion_dim, bottleneck),
                nn.GELU(),
                nn.Dropout(dropout),
                nn.Linear(bottleneck, 1),
            )
        elif compression_head_type == "simple":
            self.compression_head = nn.Sequential(
                nn.Linear(self.fused_hidden_size, bottleneck),
                nn.Tanh(),
                nn.Linear(bottleneck, 1),
            )
        else:
            raise ValueError(f"Unknown compression_head_type: {compression_head_type}")

        # Placeholder for output embedding weight reference (will be bound from backbone)
        self.embedding_weight = None
        self.token_yes_id = 9581  # Default token IDs for yes/no in Qwen vocab
        self.token_no_id = 1684

    def required_hidden_state_indices(self) -> set[int]:
        if self.use_multi_layer_fusion:
            return {
                self.early_layer_idx,
                self.middle_layer_idx,
                self.final_layer_idx,
            }
        return {self.final_layer_idx}

    def _hidden_at(
        self, hidden_states: list[mx.array | None], index: int
    ) -> mx.array:
        hidden = hidden_states[index]
        if hidden is None:
            raise RuntimeError(f"missing required hidden state at layer {index}")
        return hidden

    def __call__(
        self,
        hidden_states: list[mx.array | None],
        attention_mask: mx.array = None,
    ) -> dict[str, mx.array]:
        # 1. Extract and fuse hidden states
        if self.use_multi_layer_fusion:
            early_hidden = self._hidden_at(hidden_states, self.early_layer_idx)
            middle_hidden = self._hidden_at(hidden_states, self.middle_layer_idx)
            final_hidden = self._hidden_at(hidden_states, self.final_layer_idx)
            h = mx.concatenate([early_hidden, middle_hidden, final_hidden], axis=-1)
        else:
            h = self._hidden_at(hidden_states, -1)

        h_for_scoring = self._hidden_at(hidden_states, -1)

        # 2. Compile attention mask for MLX MultiHeadAttention
        # MLX expects a mask that is added to attention scores, where 1s (real) -> 0 and 0s (pad) -> -1e9
        if attention_mask is not None:
            mask = (1.0 - attention_mask[:, None, None, :]) * -1e9
        else:
            mask = None

        # 3. Run attention fusion layers
        for attn, norm in zip(self.fusion_layers, self.fusion_norms):
            attn_out = attn(h, h, h, mask=mask)
            h = norm(h + attn_out)

        h_compression = self.dropout(h)

        # 4. Compute compression token logits
        if self.compression_head_type == "crf":
            token_emissions = self.compression_head(h_compression)  # [B, L, 2]
            token_logits = token_emissions[:, :, 1] - token_emissions[:, :, 0]  # [B, L]
        else:
            token_logits = self.compression_head(h_compression).squeeze(-1)  # [B, L]

        # 5. Compute document-level relevance score logits
        batch_size = h_for_scoring.shape[0]
        if attention_mask is not None:
            last_token_indices = mx.sum(attention_mask, axis=1) - 1
            last_token_indices = mx.maximum(last_token_indices, 0)
        else:
            last_token_indices = mx.array([h_for_scoring.shape[1] - 1] * batch_size)

        # Gather the final active token representations
        last_hidden_for_scoring = h_for_scoring[
            mx.arange(batch_size), last_token_indices
        ]

        # Multiply by vocabulary embeddings
        if self.embedding_weight is None:
            raise ValueError("Scorer output embedding weight not initialized.")

        yes_no_weight = self.embedding_weight[
            mx.array([self.token_no_id, self.token_yes_id])
        ]
        logits_stack = mx.matmul(
            last_hidden_for_scoring, yes_no_weight.T
        )  # [B, 2], ordered as [no, yes]

        log_probs = logits_stack - mx.logsumexp(logits_stack, axis=1, keepdims=True)
        score_logits = log_probs[:, 1]

        outputs = {
            "token_logits": token_logits,
            "score_logits": score_logits,
        }
        if self.compression_head_type == "crf":
            outputs["token_emissions"] = token_emissions
        return outputs

    def decode_outputs(
        self, outputs: dict[str, mx.array], mask: mx.array = None
    ) -> mx.array:
        if self.compression_head_type == "crf":
            return self.compression_head.crf.decode(outputs["token_emissions"], mask)
        return outputs["token_logits"] > 0

    def decode(
        self, hidden_states: list[mx.array | None], mask: mx.array = None
    ) -> mx.array:
        # Full Viterbi decoding pass
        if self.use_multi_layer_fusion:
            early_hidden = self._hidden_at(hidden_states, self.early_layer_idx)
            middle_hidden = self._hidden_at(hidden_states, self.middle_layer_idx)
            final_hidden = self._hidden_at(hidden_states, self.final_layer_idx)
            h = mx.concatenate([early_hidden, middle_hidden, final_hidden], axis=-1)
        else:
            h = self._hidden_at(hidden_states, -1)

        if mask is not None:
            attn_mask = (1.0 - mask[:, None, None, :]) * -1e9
        else:
            attn_mask = None

        for attn, norm in zip(self.fusion_layers, self.fusion_norms):
            attn_out = attn(h, h, h, mask=attn_mask)
            h = norm(h + attn_out)

        h_compression = self.dropout(h)

        if self.compression_head_type == "crf":
            return self.compression_head.decode(h_compression, mask)
        else:
            token_logits = self.compression_head(h_compression).squeeze(-1)
            return token_logits > 0


class MLXSwePrunerBackend:
    def __init__(
        self, *, model_name: str, device: str = "cpu", repair: bool = False
    ) -> None:
        self.model_name = model_name
        self.device = device
        self.repair = repair  # apply AST structural repair when rendering the mask
        self.instruction = (
            "Given a query, judge if the document(code) is related to query."
        )

        if os.path.isdir(model_name):
            self.model_dir = model_name
        else:
            raise FileNotFoundError(
                "MLX backend requires a validated local model directory; "
                "run `needle model download` and load through the model catalog."
            )

        # 1. Load config from model directory
        config_path = os.path.join(self.model_dir, "config.json")
        with open(config_path, "r") as f:
            config = json.load(f)

        backbone_name = config.get(
            "backbone_model_name_or_path", "Qwen/Qwen3-Reranker-0.6B"
        )
        backbone = _resolve_backbone_reference(str(backbone_name), config)
        self._record_backbone_provenance(backbone)

        # 2. Get the backbone architecture + tokenizer.
        if _env_flag(MLX_LIGHT_ENV_NAMES, True):
            # Light path: code-pruner already ships the FULL backbone weights and
            # its own tokenizer, so build the Qwen3 architecture from the backbone's
            # tiny config.json and skip loading Qwen's ~1.2GB of weights entirely
            # (step 4 overwrites them anyway). Avoids the double-load.
            self.backbone, self.tokenizer = self._build_backbone_light(backbone)
        else:
            # Faithful path: mlx-lm loads Qwen weights, then step 4 overwrites them.
            self.backbone, tokenizer_wrapper = load(_download_backbone_snapshot(backbone))
            self.tokenizer = tokenizer_wrapper._tokenizer
        self.backbone.eval()

        self.tokenizer.padding_side = "left"

        # 3. Build TokenScorer mapping the PyTorch configurations
        num_layers = len(self.backbone.layers)
        self.scorer = MLXTokenScorer(
            hidden_size=self.backbone.model.embed_tokens.weight.shape[1],
            bottleneck=config.get("bottleneck", 256),
            dropout=config.get("dropout", 0.4),
            num_fusion_layers=config.get("num_fusion_layers", 1),
            num_heads=config.get("num_heads", 8),
            use_multi_layer_fusion=config.get("use_multi_layer_fusion", True),
            early_layer_ratio=config.get("early_layer_ratio", 0.25),
            middle_layer_ratio=config.get("middle_layer_ratio", 0.5),
            compression_head_type=config.get("compression_head_type", "crf"),
            num_hidden_layers=num_layers,
        )

        # Reference input embeddings for relevance classification
        self.scorer.embedding_weight = self.backbone.model.embed_tokens.weight
        self.scorer.token_yes_id = self.tokenizer.convert_tokens_to_ids("yes")
        self.scorer.token_no_id = self.tokenizer.convert_tokens_to_ids("no")

        # 4. Load weights from safetensors and update model states
        self._load_and_transpose_weights(self.model_dir)

        # 5. Read whether to default to Viterbi decoding (C++ implementation)
        self.use_viterbi = os.environ.get("NEEDLE_USE_VITERBI", "").lower() in {
            "1",
            "true",
            "yes",
        }
        self.last_stats: dict[str, object] = {}

    def _record_backbone_provenance(self, backbone: _ResolvedBackbone) -> None:
        from needle.model_download import augment_model_provenance

        augment_model_provenance(
            Path(self.model_dir),
            {
                "backbone_repo": backbone.repo,
                "backbone_requested_revision": backbone.requested_revision,
                "backbone_resolved_revision": backbone.resolved_revision,
                **({"backbone_path": backbone.path} if backbone.path else {}),
            },
        )

    def _build_backbone_light(self, backbone: _ResolvedBackbone):
        """Build the Qwen3 backbone architecture from its config (no Qwen
        weights) and load code-pruner's bundled tokenizer. Code-pruner's
        backbone weights are applied in step 4."""
        from huggingface_hub import hf_hub_download
        from mlx_lm.models.qwen3 import Model, ModelArgs
        from transformers import AutoTokenizer

        if backbone.path:
            config_path = os.path.join(backbone.path, "config.json")
        else:
            config_path = hf_hub_download(
                backbone.repo,
                "config.json",
                revision=backbone.resolved_revision,
            )
        with open(config_path) as f:
            backbone_config = json.load(f)
        backbone = Model(ModelArgs.from_dict(backbone_config))
        tokenizer = AutoTokenizer.from_pretrained(self.model_dir)
        return backbone, tokenizer

    def _load_and_transpose_weights(self, model_path: str):
        # Load weights dict using MLX's native loader
        weights_dict = mx.load(os.path.join(model_path, "model.safetensors"))

        backbone_weights = {}
        scorer_weights = {}

        import re

        for k, v in weights_dict.items():
            if k.startswith("model.backbone."):
                # Strip "model.backbone." and prepend "model." to match MLX Qwen3 structure
                name = "model." + k[len("model.backbone.") :]
                backbone_weights[name] = v
            elif k.startswith("model."):
                name = k[len("model.") :]

                # Split PyTorch's combined in_proj_weight and in_proj_bias for MLX MultiHeadAttention
                if "in_proj_weight" in name:
                    # Shape: [3 * embed_dim, embed_dim]
                    q_w, k_w, v_w = mx.split(v, 3, axis=0)
                    prefix = name[: -len("in_proj_weight")]
                    scorer_weights[prefix + "query_proj.weight"] = q_w
                    scorer_weights[prefix + "key_proj.weight"] = k_w
                    scorer_weights[prefix + "value_proj.weight"] = v_w
                elif "in_proj_bias" in name:
                    # Shape: [3 * embed_dim]
                    q_b, k_b, v_b = mx.split(v, 3, axis=0)
                    prefix = name[: -len("in_proj_bias")]
                    scorer_weights[prefix + "query_proj.bias"] = q_b
                    scorer_weights[prefix + "key_proj.bias"] = k_b
                    scorer_weights[prefix + "value_proj.bias"] = v_b
                else:
                    # Fix PyTorch Sequential vs MLX Sequential naming mismatch
                    # PyTorch: compression_head.feature_extractor.0.weight
                    # MLX:     compression_head.feature_extractor.layers.0.weight
                    name = re.sub(
                        r"feature_extractor\.(\d+)",
                        r"feature_extractor.layers.\1",
                        name,
                    )
                    name = re.sub(
                        r"compression_head\.(\d+)", r"compression_head.layers.\1", name
                    )
                    scorer_weights[name] = v

        # Convert flat dotted dictionaries to MLX nested dictionaries
        from mlx.utils import tree_unflatten

        nested_backbone_weights = tree_unflatten(list(backbone_weights.items()))
        nested_scorer_weights = tree_unflatten(list(scorer_weights.items()))

        # Update the models
        self.backbone.update(nested_backbone_weights)
        self.scorer.update(nested_scorer_weights)

    def _prompt_token_ids(self, query: str) -> tuple[list[int], list[int], list[int]]:
        formatted_query = (
            f"<Instruct>: {self.instruction}\n<Query>: {query}\n<Document>: "
        )
        prefix_ids = self.tokenizer.encode(_PROMPT_PREFIX, add_special_tokens=False)
        suffix_ids = self.tokenizer.encode(_PROMPT_SUFFIX, add_special_tokens=False)
        query_ids = self.tokenizer.encode(formatted_query, add_special_tokens=False)
        return prefix_ids, query_ids, suffix_ids

    def _prepare_chunk_row(
        self,
        *,
        chunk: TokenChunk,
        prefix_ids: list[int],
        query_ids: list[int],
        suffix_ids: list[int],
        max_length: int,
    ) -> _PreparedChunk:
        if chunk.token_count and not chunk.token_ids:
            raise ValueError("TokenChunk is missing preserved token ids")
        if chunk.token_count and not chunk.token_offsets:
            raise ValueError("TokenChunk is missing preserved token offsets")

        code_ids = list(chunk.token_ids)
        code_offsets = chunk.relative_token_offsets
        original_code_tokens = len(code_ids)

        available_len = max_length - len(prefix_ids) - len(suffix_ids) - len(query_ids)
        if len(code_ids) > available_len:
            code_ids = code_ids[:available_len]
            code_offsets = code_offsets[:available_len]

        doc_start = len(prefix_ids) + len(query_ids)
        doc_end = doc_start + len(code_ids)
        input_ids = prefix_ids + query_ids + code_ids + suffix_ids
        return _PreparedChunk(
            chunk=chunk,
            input_ids=input_ids,
            code_offsets=code_offsets,
            doc_start=doc_start,
            doc_end=doc_end,
            original_code_tokens=original_code_tokens,
        )

    def _score_prepared_batch(
        self,
        prepared: list[_PreparedChunk],
        *,
        max_length: int,
        use_viterbi: bool,
        profile_forced_eval: bool,
    ) -> tuple[
        list[tuple[float, list[tuple[str, float]], list[tuple[int, int]], int]],
        dict[str, object],
    ]:
        total_start = time.perf_counter()
        memory_start: dict[str, float] = {}
        if profile_forced_eval:
            _reset_mlx_peak_memory()
            memory_start = _mlx_memory_mb()
        if not prepared:
            return [], {
                "chunks": 0,
                "batches": 0,
                "max_length": max_length,
                "real_tokens": 0,
                "padded_tokens": 0,
                "pad_tokens": 0,
                "padding_waste_ratio": 0.0,
            }

        batch_len = max(item.real_len for item in prepared)
        pad_id = self.tokenizer.pad_token_id
        input_rows: list[list[int]] = []
        mask_rows: list[list[int]] = []
        for item in prepared:
            pad_len = batch_len - item.real_len
            input_rows.append(item.input_ids + [pad_id] * pad_len)
            mask_rows.append([1] * item.real_len + [0] * pad_len)

        input_ids_mx = mx.array(input_rows)
        attention_mask_mx = mx.array(mask_rows)

        graph_start = time.perf_counter()
        layers = self.backbone.model.layers
        required_hidden = self.scorer.required_hidden_state_indices()
        hidden_states: list[mx.array | None] = [None] * (len(layers) + 1)
        h = self.backbone.model.embed_tokens(input_ids_mx)
        if 0 in required_hidden:
            hidden_states[0] = h

        import sys

        model_module_name = self.backbone.model.__class__.__module__
        model_module = sys.modules[model_module_name]
        create_attention_mask = getattr(model_module, "create_attention_mask")
        mask = create_attention_mask(h, None)

        for layer_idx, layer in enumerate(layers, start=1):
            h = layer(h, mask=mask)
            if layer_idx in required_hidden:
                hidden_states[layer_idx] = h

        h = self.backbone.model.norm(h)
        hidden_states[-1] = h

        outputs = self.scorer(hidden_states, attention_mask_mx)
        graph_build_ms = (time.perf_counter() - graph_start) * 1000
        forward_eval_ms = None
        if profile_forced_eval:
            eval_start = time.perf_counter()
            to_eval = [outputs["token_logits"], outputs["score_logits"]]
            if "token_emissions" in outputs:
                to_eval.append(outputs["token_emissions"])
            _force_mlx_eval(*to_eval)
            forward_eval_ms = (time.perf_counter() - eval_start) * 1000

        decode_start = time.perf_counter()
        if use_viterbi:
            best_paths = self.scorer.decode_outputs(outputs, attention_mask_mx)
        else:
            best_paths = None
        decode_graph_ms = (time.perf_counter() - decode_start) * 1000

        host_sync_start = time.perf_counter()
        score_probs = mx.exp(outputs["score_logits"])
        score_probs_np = np.array(score_probs.astype(mx.float32), dtype=np.float32)

        results: list[tuple[float, list[tuple[str, float]], list[tuple[int, int]], int]] = []
        for row_idx, item in enumerate(prepared):
            if use_viterbi:
                probs = best_paths[row_idx, item.doc_start : item.doc_end].astype(mx.float32)
            else:
                token_logits_seq = outputs["token_logits"][
                    row_idx, item.doc_start : item.doc_end
                ]
                probs = mx.sigmoid(token_logits_seq)
            probs_np = np.array(probs.astype(mx.float32), dtype=np.float32)
            token_scores = [("", float(score)) for score in probs_np.tolist()]
            results.append(
                (
                    float(score_probs_np[row_idx]),
                    token_scores,
                    item.code_offsets,
                    item.chunk.start_char,
                )
            )
        host_sync_ms = (time.perf_counter() - host_sync_start) * 1000
        memory_end = _mlx_memory_mb() if profile_forced_eval else {}

        real_tokens = sum(item.real_len for item in prepared)
        padded_tokens = len(prepared) * batch_len
        pad_tokens = padded_tokens - real_tokens
        stats = {
            "chunks": len(prepared),
            "batches": 1,
            "max_length": max_length,
            "batch_size": len(prepared),
            "batch_length": batch_len,
            "original_code_tokens": sum(
                item.original_code_tokens for item in prepared
            ),
            "code_tokens": sum(item.code_tokens for item in prepared),
            "truncated_code_tokens": sum(
                max(0, item.original_code_tokens - item.code_tokens)
                for item in prepared
            ),
            "real_tokens": real_tokens,
            "padded_tokens": padded_tokens,
            "pad_tokens": pad_tokens,
            "padding_waste_ratio": pad_tokens / padded_tokens
            if padded_tokens
            else 0.0,
            "profile_forced_eval": profile_forced_eval,
            "retained_hidden_states": len(required_hidden),
            "available_hidden_states": len(layers) + 1,
            "graph_build_ms": graph_build_ms,
            "forward_eval_ms": forward_eval_ms,
            "decode_graph_ms": decode_graph_ms,
            "host_sync_ms": host_sync_ms,
            "batch_total_ms": (time.perf_counter() - total_start) * 1000,
        }
        if memory_start or memory_end:
            stats.update(
                {
                    "mlx_active_mb_start": memory_start.get("active_mb"),
                    "mlx_cache_mb_start": memory_start.get("cache_mb"),
                    "mlx_peak_mb_start": memory_start.get("peak_mb"),
                    "mlx_active_mb_end": memory_end.get("active_mb"),
                    "mlx_cache_mb_end": memory_end.get("cache_mb"),
                    "mlx_peak_mb_end": memory_end.get("peak_mb"),
                }
            )
        return results, stats

    def _process_single_chunk(
        self,
        query: str,
        code_chunk: str,
        max_length: int = 8192,
        use_viterbi: bool = False,
    ):
        total_start = time.perf_counter()
        profile_forced_eval = _env_flag(PROFILE_MLX_ENV_NAMES, False)

        # Format instruction prompt
        tokenize_start = time.perf_counter()
        prefix = '<|im_start|>system\nJudge whether the Document meets the requirements based on the Query and the Instruct provided. Note that the answer can only be "yes" or "no".<|im_end|>\n<|im_start|>user\n'
        suffix = "<|im_end|>\n<|im_start|>assistant\n<think>\n\n</think>\n\n"
        formatted_query = (
            f"<Instruct>: {self.instruction}\n<Query>: {query}\n<Document>: "
        )

        # Tokenize
        prefix_ids = self.tokenizer.encode(prefix, add_special_tokens=False)
        suffix_ids = self.tokenizer.encode(suffix, add_special_tokens=False)
        query_ids = self.tokenizer.encode(formatted_query, add_special_tokens=False)

        code_enc = self.tokenizer(
            code_chunk,
            add_special_tokens=False,
            truncation=False,
            return_attention_mask=False,
            return_offsets_mapping=True,
        )
        code_ids = code_enc["input_ids"]
        code_offsets = code_enc["offset_mapping"]
        original_code_tokens = len(code_ids)

        # Truncate code if sequence exceeds max length
        available_len = max_length - len(prefix_ids) - len(suffix_ids) - len(query_ids)
        if len(code_ids) > available_len:
            code_ids = code_ids[:available_len]
            code_offsets = code_offsets[:available_len]
        tokenize_ms = (time.perf_counter() - tokenize_start) * 1000

        input_ids = prefix_ids + query_ids + code_ids + suffix_ids
        real_len = len(input_ids)

        # Padding (right padding matching PyTorch)
        pad_len = max_length - real_len
        input_ids = input_ids + [self.tokenizer.pad_token_id] * pad_len
        attention_mask = [1] * real_len + [0] * pad_len

        doc_start = len(prefix_ids) + len(query_ids)
        doc_end = doc_start + len(code_ids)

        # Convert to MLX arrays
        input_ids_mx = mx.array([input_ids])
        attention_mask_mx = mx.array([attention_mask])

        # Run Backbone to collect hidden states
        graph_start = time.perf_counter()
        layers = self.backbone.model.layers
        required_hidden = self.scorer.required_hidden_state_indices()
        hidden_states: list[mx.array | None] = [None] * (len(layers) + 1)

        # We hook into layers output by running the layers sequentially
        # First, run the embedding layer
        h = self.backbone.model.embed_tokens(input_ids_mx)
        if 0 in required_hidden:
            hidden_states[0] = h

        # Get the appropriate create_attention_mask helper for the loaded model architecture
        import sys

        model_module_name = self.backbone.model.__class__.__module__
        model_module = sys.modules[model_module_name]
        create_attention_mask = getattr(model_module, "create_attention_mask")
        mask = create_attention_mask(h, None)

        # Run layers one by one to collect hidden states at each step
        for layer_idx, layer in enumerate(layers, start=1):
            h = layer(h, mask=mask)
            if layer_idx in required_hidden:
                hidden_states[layer_idx] = h

        # Run final norm
        h = self.backbone.model.norm(h)
        hidden_states[-1] = h

        # Run Custom TokenScorer once; Viterbi can decode from the same emissions.
        outputs = self.scorer(hidden_states, attention_mask_mx)
        graph_build_ms = (time.perf_counter() - graph_start) * 1000
        forward_eval_ms = None
        if profile_forced_eval:
            eval_start = time.perf_counter()
            to_eval = [outputs["token_logits"], outputs["score_logits"]]
            if "token_emissions" in outputs:
                to_eval.append(outputs["token_emissions"])
            _force_mlx_eval(*to_eval)
            forward_eval_ms = (time.perf_counter() - eval_start) * 1000

        decode_start = time.perf_counter()
        if use_viterbi:
            best_paths = self.scorer.decode_outputs(outputs, attention_mask_mx)
            probs = best_paths[0, doc_start:doc_end].astype(mx.float32)
        else:
            # Calculate probability scores using sigmoid on token logits
            token_logits_seq = outputs["token_logits"][0, doc_start:doc_end]
            probs = mx.sigmoid(token_logits_seq)
        decode_graph_ms = (time.perf_counter() - decode_start) * 1000

        # Calculate final relevance score (exp since it is log_softmax)
        host_sync_start = time.perf_counter()
        score_prob = mx.exp(outputs["score_logits"][0])
        chunk_score = float(score_prob.astype(mx.float32).item())

        # The line aggregator only needs scores plus offsets, so avoid per-token
        # string conversion and host syncs.
        probs_np = np.array(probs.astype(mx.float32), dtype=np.float32)
        code_token_scores = [("", float(score)) for score in probs_np.tolist()]
        host_sync_ms = (time.perf_counter() - host_sync_start) * 1000

        stats = {
            "chunks": 1,
            "batches": 1,
            "max_length": max_length,
            "available_code_tokens": available_len,
            "original_code_tokens": original_code_tokens,
            "code_tokens": len(code_ids),
            "truncated_code_tokens": max(0, original_code_tokens - len(code_ids)),
            "prefix_tokens": len(prefix_ids),
            "query_tokens": len(query_ids),
            "suffix_tokens": len(suffix_ids),
            "real_tokens": real_len,
            "padded_tokens": len(input_ids),
            "pad_tokens": pad_len,
            "padding_waste_ratio": pad_len / len(input_ids) if input_ids else 0.0,
            "profile_forced_eval": profile_forced_eval,
            "tokenize_ms": tokenize_ms,
            "graph_build_ms": graph_build_ms,
            "forward_eval_ms": forward_eval_ms,
            "decode_graph_ms": decode_graph_ms,
            "host_sync_ms": host_sync_ms,
            "chunk_total_ms": (time.perf_counter() - total_start) * 1000,
        }

        return chunk_score, code_token_scores, code_offsets, stats

    def evict(self) -> None:
        """Release any cached device memory held by the backend. Idempotent."""
        try:
            _clear_mlx_cache()
        except Exception:  # noqa: BLE001
            pass

    def prune_text(
        self,
        *,
        text: str,
        query: str,
        threshold: float,
        max_length: int,
        use_viterbi: bool | None = None,
    ) -> str:
        if use_viterbi is None:
            use_viterbi = self.use_viterbi

        total_start = time.perf_counter()
        profile_forced_eval = _env_flag(PROFILE_MLX_ENV_NAMES, False)
        tokenize_start = time.perf_counter()
        prefix_ids, query_ids, suffix_ids = self._prompt_token_ids(query)
        prompt_tokens = len(prefix_ids) + len(query_ids) + len(suffix_ids)
        original_tokens = estimate_token_count(text, self.tokenizer)
        if max_length <= 0:
            max_length, max_length_profile = choose_mlx_max_length(
                original_tokens=original_tokens,
                prompt_tokens=prompt_tokens,
                min_code_tokens=_MIN_CODE_TOKENS,
            )
        else:
            max_length_profile = "fixed"
        code_token_budget = max_length - prompt_tokens
        base_stats: dict[str, object] = {
            "original_tokens": original_tokens,
            "chunks": 0,
            "batches": 0,
            "max_length": max_length,
            "max_length_profile": max_length_profile,
            "available_code_tokens": code_token_budget,
            "prefix_tokens": len(prefix_ids),
            "query_tokens": len(query_ids),
            "suffix_tokens": len(suffix_ids),
            "profile_forced_eval": profile_forced_eval,
            "chunked": False,
            "batched": False,
        }
        if code_token_budget < _MIN_CODE_TOKENS:
            self.last_stats = {
                **base_stats,
                "passthrough_reason": "query-too-long",
                "input_chars": len(text),
                "output_chars": len(text),
                "saved_chars": 0,
                "total_ms": (time.perf_counter() - total_start) * 1000,
            }
            return text

        overlap_tokens = int(first_env(CHUNK_OVERLAP_ENV_NAMES, default="50"))
        max_batch_size = int(first_env(MAX_BATCH_SIZE_ENV_NAMES, default="1"))
        max_batch_tokens = configured_max_batch_tokens()
        max_length_ratio = float(first_env(MAX_LENGTH_RATIO_ENV_NAMES, default="1.5"))

        chunks = split_text_into_token_chunks(
            text,
            self.tokenizer,
            chunk_max_tokens=code_token_budget,
            overlap_tokens=overlap_tokens,
        )
        if not chunks:
            self.last_stats = {
                **base_stats,
                "passthrough_reason": "empty-tokenization",
                "input_chars": len(text),
                "output_chars": len(text),
                "saved_chars": 0,
                "total_ms": (time.perf_counter() - total_start) * 1000,
            }
            return text

        chunk_batches = bucket_token_chunks(
            chunks,
            max_batch_size=max_batch_size,
            max_length_ratio=max_length_ratio,
        )
        prepared_batches = [
            [
                self._prepare_chunk_row(
                    chunk=chunk,
                    prefix_ids=prefix_ids,
                    query_ids=query_ids,
                    suffix_ids=suffix_ids,
                    max_length=max_length,
                )
                for chunk in batch
            ]
            for batch in chunk_batches
        ]
        budgeted = split_batches_by_padded_token_budget(
            prepared_batches,
            max_padded_tokens=max_batch_tokens,
            length_fn=lambda item: item.real_len,
        )
        prepared_batches = budgeted.batches
        tokenize_ms = (time.perf_counter() - tokenize_start) * 1000

        scored_results: list[
            tuple[float, list[tuple[str, float]], list[tuple[int, int]], int]
        ] = []
        batch_stats: list[dict[str, object]] = []

        def score_batch(prepared: list[_PreparedChunk]):
            return self._score_prepared_batch(
                prepared,
                max_length=max_length,
                use_viterbi=use_viterbi,
                profile_forced_eval=profile_forced_eval,
            )

        batch_retry_summary: dict[str, object] = {"batch_retry_count": 0}
        try:
            scored_results, batch_stats, batch_retry_summary = score_batches_with_retry(
                prepared_batches,
                score_batch,
                _is_mlx_resource_error,
            )
        except BatchRetryFailed as exc:
            self.last_stats = {
                **base_stats,
                "passthrough_reason": "batch-resource-error",
                "batch_error": str(exc.original)[:200],
                "chunks": len(chunks),
                "batches": len(prepared_batches),
                "max_batch_size": max_batch_size,
                "max_batch_tokens": max_batch_tokens,
                "batch_guardrail_splits": budgeted.splits,
                "batch_guardrail_singles_over_budget": budgeted.singles_over_budget,
                "input_chars": len(text),
                "output_chars": len(text),
                "saved_chars": 0,
                "total_ms": (time.perf_counter() - total_start) * 1000,
                **exc.summary,
            }
            return text

        if not scored_results:
            self.last_stats = {
                **base_stats,
                "passthrough_reason": "no-scored-chunks",
                "chunks": len(chunks),
                "batches": len(prepared_batches),
                "max_batch_size": max_batch_size,
                "max_batch_tokens": max_batch_tokens,
                "batch_guardrail_splits": budgeted.splits,
                "batch_guardrail_singles_over_budget": budgeted.singles_over_budget,
                "input_chars": len(text),
                "output_chars": len(text),
                "saved_chars": 0,
                "total_ms": (time.perf_counter() - total_start) * 1000,
            }
            return text

        code_token_scores, code_offsets = merge_token_scores_from_chunks(
            text,
            [
                (token_scores, offsets, start_char)
                for _score, token_scores, offsets, start_char in scored_results
            ],
        )

        # Map token-level scores back to line numbers
        aggregate_start = time.perf_counter()
        line_scores = aggregate_token_scores_to_lines(
            text, code_token_scores, code_offsets
        )
        line_aggregate_ms = (time.perf_counter() - aggregate_start) * 1000

        render_start = time.perf_counter()
        pruned, kept_lines = prune_code_lines(text, line_scores, threshold, False)

        # Optional AST structural repair (off by default): an alternative render
        # of the kept-line mask that pulls in enclosing scopes/imports so the
        # output still parses. Gated so the model stands on its own; flip on for
        # evals via the backend's `repair` flag (NEEDLE_REPAIR, or legacy
        # compatibility HAY_REPAIR).
        result = pruned
        if self.repair and _looks_like_python(text):
            try:
                from .repair import repair_python_mask

                result = repair_python_mask(text, kept_lines).repaired_code
            except SyntaxError:
                result = pruned

        result = _apply_floor(text, result)
        real_tokens = sum(int(stats.get("real_tokens", 0)) for stats in batch_stats)
        padded_tokens = sum(int(stats.get("padded_tokens", 0)) for stats in batch_stats)
        pad_tokens = padded_tokens - real_tokens
        forward_values = [
            float(stats["forward_eval_ms"])
            for stats in batch_stats
            if stats.get("forward_eval_ms") is not None
        ]
        retained_hidden_values = [
            int(stats["retained_hidden_states"])
            for stats in batch_stats
            if stats.get("retained_hidden_states") is not None
        ]
        available_hidden_values = [
            int(stats["available_hidden_states"])
            for stats in batch_stats
            if stats.get("available_hidden_states") is not None
        ]
        mlx_active_end_values = [
            float(stats["mlx_active_mb_end"])
            for stats in batch_stats
            if stats.get("mlx_active_mb_end") is not None
        ]
        mlx_cache_end_values = [
            float(stats["mlx_cache_mb_end"])
            for stats in batch_stats
            if stats.get("mlx_cache_mb_end") is not None
        ]
        mlx_peak_values = [
            float(stats["mlx_peak_mb_end"])
            for stats in batch_stats
            if stats.get("mlx_peak_mb_end") is not None
        ]
        chunk_scores = [score for score, _tokens, _offsets, _start in scored_results]
        self.last_stats = {
            **base_stats,
            "chunks": len(chunks),
            "batches": len(batch_stats),
            "batch_sizes": [len(batch) for batch in prepared_batches],
            "max_batch_size": max_batch_size,
            "max_batch_tokens": max_batch_tokens,
            "max_length_ratio": max_length_ratio,
            "batch_guardrail_splits": budgeted.splits,
            "batch_guardrail_singles_over_budget": budgeted.singles_over_budget,
            **batch_retry_summary,
            "chunk_overlap_tokens": overlap_tokens,
            "chunked": len(chunks) > 1,
            "batched": any(len(batch) > 1 for batch in prepared_batches),
            "original_code_tokens": original_tokens,
            "scored_code_tokens": sum(
                int(stats.get("code_tokens", 0)) for stats in batch_stats
            ),
            "truncated_code_tokens": sum(
                int(stats.get("truncated_code_tokens", 0)) for stats in batch_stats
            ),
            "real_tokens": real_tokens,
            "padded_tokens": padded_tokens,
            "pad_tokens": pad_tokens,
            "padding_waste_ratio": pad_tokens / padded_tokens
            if padded_tokens
            else 0.0,
            "max_chunk_score": max(chunk_scores) if chunk_scores else 0.0,
            "tokenize_ms": tokenize_ms,
            "graph_build_ms": sum(
                float(stats.get("graph_build_ms", 0.0)) for stats in batch_stats
            ),
            "forward_eval_ms": sum(forward_values) if forward_values else None,
            "decode_graph_ms": sum(
                float(stats.get("decode_graph_ms", 0.0)) for stats in batch_stats
            ),
            "host_sync_ms": sum(
                float(stats.get("host_sync_ms", 0.0)) for stats in batch_stats
            ),
            "batch_total_ms": sum(
                float(stats.get("batch_total_ms", 0.0)) for stats in batch_stats
            ),
            "retained_hidden_states": max(retained_hidden_values)
            if retained_hidden_values
            else None,
            "available_hidden_states": max(available_hidden_values)
            if available_hidden_values
            else None,
            "mlx_active_mb_end": mlx_active_end_values[-1]
            if mlx_active_end_values
            else None,
            "mlx_cache_mb_end": mlx_cache_end_values[-1]
            if mlx_cache_end_values
            else None,
            "mlx_peak_mb_max": max(mlx_peak_values) if mlx_peak_values else None,
            "input_chars": len(text),
            "output_chars": len(result),
            "saved_chars": max(0, len(text) - len(result)),
            "line_aggregate_ms": line_aggregate_ms,
            "render_ms": (time.perf_counter() - render_start) * 1000,
            "total_ms": (time.perf_counter() - total_start) * 1000,
        }
        return result


def _looks_like_python(text: str) -> bool:
    import ast

    if "def " in text or "class " in text or "import " in text:
        try:
            ast.parse(_without_filter_markers(text))
        except SyntaxError:
            return False
        return True
    return False


def _real_content_chars(pruned: str) -> int:
    """Chars of actual kept code, ignoring filtered placeholder lines."""
    return sum(
        len(line)
        for line in pruned.splitlines()
        if line.strip() and not _is_filter_marker(line)
    )


def _python_skeleton(text: str) -> str:
    """Imports + top-level/class def/class signatures (and decorators), bodies
    dropped. The graceful floor when the model would otherwise nuke a whole file
    to a placeholder: the agent still sees the file's shape, not nothing."""
    keep = ("import ", "from ", "def ", "async def ", "class ", "@")
    out: list[str] = []
    gap = False
    for line in text.splitlines():
        if line.strip().startswith(keep):
            out.append(line)
            gap = False
        elif out and not gap:
            out.append("[pruned]")
            gap = True
    return "\n".join(out)


def _apply_floor(text: str, pruned: str) -> str:
    """Don't return near-nothing. If pruning collapsed the file (<5% real content
    kept), fall back to a signatures skeleton (Python) or pass the original
    through, so a deliberate read never comes back empty."""
    if _real_content_chars(pruned) >= 0.05 * len(text):
        return pruned
    if _looks_like_python(text):
        skeleton = _python_skeleton(text)
        if _real_content_chars(skeleton) > _real_content_chars(pruned):
            return skeleton
    return text  # non-Python or no useful skeleton: pass through, never empty


def _without_filter_markers(text: str) -> str:
    return "\n".join(
        "pass" if _is_filter_marker(line) else line
        for line in text.splitlines()
    )


def _is_filter_marker(line: str) -> bool:
    stripped = line.strip()
    return stripped.startswith("[pruned") or stripped.startswith("(filtered ")


# ---------------------------------------------------------------------------
# Needle wrapper: a sealed backend implementing the `prune(text, query)` backend
# protocol. Everything above this line is the ML port from code-pruner; the
# wrapper below is the only part that talks to the rest of Needle.
# ---------------------------------------------------------------------------


def _resolve_model_dir() -> str:
    """Local directory for the code-pruner model.

    NEEDLE_MODEL_DIR points at an exact existing model directory. Otherwise
    Needle downloads NEEDLE_MODEL from Hugging Face into a Needle-owned
    directory so uninstall can clean up without spelunking through the shared
    Hugging Face cache. HAY_* names are still accepted as legacy compatibility
    fallbacks.
    """
    explicit = os.environ.get("NEEDLE_MODEL_DIR") or os.environ.get("HAY_MODEL_DIR")
    if explicit:
        return explicit
    from needle.model_download import download_model_snapshot

    repo = os.environ.get("NEEDLE_MODEL") or os.environ.get("HAY_MODEL", "ayanami-kitasan/code-pruner")
    revision = os.environ.get("NEEDLE_MODEL_REVISION") or os.environ.get("HAY_MODEL_REVISION")
    result = download_model_snapshot(
        repo=repo,
        revision=revision,
        caller="runtime",
        force=False,
    )
    return result.path


class CodePrunerBackend:
    """The real SWE-pruner / code-pruner relevance model on MLX. Sealed: the
    rest of Needle only sees prune(text, query) -> str. The MLX classes above are
    the *implementation*; the backend's identity is the model (code-pruner)."""

    name = "code-pruner"

    def __init__(self, model_dir: str | None = None) -> None:
        repair = repair_enabled_for_active_package()
        _set_mlx_limit("set_cache_limit", _env_mb(MLX_CACHE_LIMIT_ENV_NAMES))
        _set_mlx_limit("set_wired_limit", _env_mb(MLX_WIRED_LIMIT_ENV_NAMES))
        self._clear_cache_after_prune = _env_flag(MLX_CLEAR_CACHE_ENV_NAMES, True)
        self._impl = MLXSwePrunerBackend(
            model_name=model_dir or _resolve_model_dir(), repair=repair
        )
        self._threshold = float(first_env(THRESHOLD_ENV_NAMES, default="0.5"))
        self._max_length = configured_max_length() or 0

    def prune(self, *, text: str, query: str) -> str:
        try:
            return self._impl.prune_text(
                text=text,
                query=query,
                threshold=self._threshold,
                max_length=self._max_length,
            )
        finally:
            if self._clear_cache_after_prune:
                _clear_mlx_cache()

    @property
    def last_stats(self) -> dict[str, object]:
        return dict(self._impl.last_stats)

    def evict(self) -> None:
        self._impl.evict()
