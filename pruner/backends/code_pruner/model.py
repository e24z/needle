import json
import os

import mlx.core as mx
import mlx.nn as nn
import numpy as np
from mlx_lm import load

from ... import naming
from .lines import aggregate_token_scores_to_lines, prune_code_lines

# The optional C++ Viterbi extension never shipped to Hay; the numpy decoder
# below is the only path. (Original lives in ~/repos/needle if ever worth porting.)
viterbi_cpp = None


def _mlx_func(name: str):
    fn = getattr(mx, name, None)
    if fn is not None:
        return fn
    metal = getattr(mx, "metal", None)
    return getattr(metal, name, None) if metal is not None else None


def _env_flag(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.lower() not in {"0", "false", "no", "off"}


def _env_mb(name: str) -> int | None:
    value = os.environ.get(name)
    if not value:
        return None
    try:
        mb = int(value)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer number of MB") from exc
    if mb < 0:
        raise ValueError(f"{name} must be non-negative")
    return mb


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

        if self.use_multi_layer_fusion:
            self.early_layer_idx = max(1, int(num_hidden_layers * early_layer_ratio))
            self.middle_layer_idx = max(1, int(num_hidden_layers * middle_layer_ratio))
            self.final_layer_idx = num_hidden_layers
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

    def __call__(
        self,
        hidden_states: list[mx.array],
        attention_mask: mx.array = None,
    ) -> dict[str, mx.array]:
        # 1. Extract and fuse hidden states
        if self.use_multi_layer_fusion:
            early_hidden = hidden_states[self.early_layer_idx]
            middle_hidden = hidden_states[self.middle_layer_idx]
            final_hidden = hidden_states[self.final_layer_idx]
            h = mx.concatenate([early_hidden, middle_hidden, final_hidden], axis=-1)
        else:
            h = hidden_states[-1]

        h_for_scoring = hidden_states[-1]

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

    def decode(self, hidden_states: list[mx.array], mask: mx.array = None) -> mx.array:
        # Full Viterbi decoding pass
        if self.use_multi_layer_fusion:
            early_hidden = hidden_states[self.early_layer_idx]
            middle_hidden = hidden_states[self.middle_layer_idx]
            final_hidden = hidden_states[self.final_layer_idx]
            h = mx.concatenate([early_hidden, middle_hidden, final_hidden], axis=-1)
        else:
            h = hidden_states[-1]

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

        # 2. Get the backbone architecture + tokenizer.
        if os.environ.get("HAY_MLX_LIGHT", "1").lower() in {"1", "true", "yes"}:
            # Light path: code-pruner already ships the FULL backbone weights and
            # its own tokenizer, so build the Qwen3 architecture from the backbone's
            # tiny config.json and skip loading Qwen's ~1.2GB of weights entirely
            # (step 4 overwrites them anyway). Avoids the double-load.
            self.backbone, self.tokenizer = self._build_backbone_light(backbone_name)
        else:
            # Faithful path: mlx-lm loads Qwen weights, then step 4 overwrites them.
            self.backbone, tokenizer_wrapper = load(backbone_name)
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

    def _build_backbone_light(self, backbone_name: str):
        """Build the Qwen3 backbone architecture from its config (no Qwen
        weights) and load code-pruner's bundled tokenizer. Code-pruner's
        backbone weights are applied in step 4."""
        from huggingface_hub import hf_hub_download
        from mlx_lm.models.qwen3 import Model, ModelArgs
        from transformers import AutoTokenizer

        with open(hf_hub_download(backbone_name, "config.json")) as f:
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

    def _process_single_chunk(
        self,
        query: str,
        code_chunk: str,
        max_length: int = 8192,
        use_viterbi: bool = False,
    ):
        # Format instruction prompt
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

        # Truncate code if sequence exceeds max length
        available_len = max_length - len(prefix_ids) - len(suffix_ids) - len(query_ids)
        if len(code_ids) > available_len:
            code_ids = code_ids[:available_len]
            code_offsets = code_offsets[:available_len]

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
        hidden_states = []

        # We hook into layers output by running the layers sequentially
        # First, run the embedding layer
        h = self.backbone.model.embed_tokens(input_ids_mx)
        hidden_states.append(h)

        # Get the appropriate create_attention_mask helper for the loaded model architecture
        import sys

        model_module_name = self.backbone.model.__class__.__module__
        model_module = sys.modules[model_module_name]
        create_attention_mask = getattr(model_module, "create_attention_mask")
        mask = create_attention_mask(h, None)

        # Run layers one by one to collect hidden states at each step
        for layer in self.backbone.model.layers:
            h = layer(h, mask=mask)
            hidden_states.append(h)

        # Run final norm
        h = self.backbone.model.norm(h)
        hidden_states[-1] = h

        # Run Custom TokenScorer once; Viterbi can decode from the same emissions.
        outputs = self.scorer(hidden_states, attention_mask_mx)
        if use_viterbi:
            best_paths = self.scorer.decode_outputs(outputs, attention_mask_mx)
            probs = best_paths[0, doc_start:doc_end].astype(mx.float32)
        else:
            # Calculate probability scores using sigmoid on token logits
            token_logits_seq = outputs["token_logits"][0, doc_start:doc_end]
            probs = mx.sigmoid(token_logits_seq)

        # Calculate final relevance score (exp since it is log_softmax)
        score_prob = mx.exp(outputs["score_logits"][0])
        chunk_score = float(score_prob.astype(mx.float32).item())

        # The line aggregator only needs scores plus offsets, so avoid per-token
        # string conversion and host syncs.
        probs_np = np.array(probs.astype(mx.float32), dtype=np.float32)
        code_token_scores = [("", float(score)) for score in probs_np.tolist()]

        return chunk_score, code_token_scores, code_offsets

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

        # Process single code chunk
        chunk_score, code_token_scores, code_offsets = self._process_single_chunk(
            query, text, max_length=max_length, use_viterbi=use_viterbi
        )

        # Map token-level scores back to line numbers
        line_scores = aggregate_token_scores_to_lines(
            text, code_token_scores, code_offsets
        )
        pruned, kept_lines = prune_code_lines(text, line_scores, threshold, False)

        # Optional AST structural repair (off by default): an alternative render
        # of the kept-line mask that pulls in enclosing scopes/imports so the
        # output still parses. Gated so the model stands on its own; flip on for
        # evals via the backend's `repair` flag (HAY_REPAIR).
        result = pruned
        if self.repair and _looks_like_python(text):
            try:
                from .repair import repair_python_mask

                result = repair_python_mask(text, kept_lines).repaired_code
            except SyntaxError:
                result = pruned

        return _apply_floor(text, result)


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
    """Chars of actual kept code, ignoring the [pruned ...] placeholder lines."""
    return sum(
        len(line)
        for line in pruned.splitlines()
        if line.strip() and not line.strip().startswith("[pruned")
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
        "pass" if line.strip().startswith("[pruned ") else line
        for line in text.splitlines()
    )


# ---------------------------------------------------------------------------
# Hay wrapper: a sealed backend implementing pruner's `prune(text, query)`.
# Everything above this line is ML ported verbatim from needle. The wrapper
# below is the only part that talks to the rest of Hay.
# ---------------------------------------------------------------------------


def _resolve_model_dir() -> str:
    """Local directory for the code-pruner model.

    HAY_MODEL_DIR points at an exact existing model directory. Otherwise Hay
    downloads HAY_MODEL from Hugging Face into a Hay-owned directory so uninstall
    can clean up without spelunking through the shared Hugging Face cache.
    """
    explicit = os.environ.get("HAY_MODEL_DIR")
    if explicit:
        return explicit
    from huggingface_hub import snapshot_download

    repo = os.environ.get("HAY_MODEL", "ayanami-kitasan/code-pruner")
    root = naming.model_root()
    local_dir = naming.model_dir_for_repo(repo)
    root.mkdir(parents=True, exist_ok=True)
    return snapshot_download(
        repo,
        local_dir=str(local_dir),
        cache_dir=str(root / ".hf-cache"),
    )


class CodePrunerBackend:
    """The real SWE-pruner / code-pruner relevance model on MLX. Sealed: the
    rest of Hay only sees prune(text, query) -> str. The MLX classes above are
    the *implementation*; the backend's identity is the model (code-pruner)."""

    name = "code-pruner"

    def __init__(self, model_dir: str | None = None) -> None:
        repair = os.environ.get("HAY_REPAIR", "1").lower() not in {"0", "false", "no"}
        _set_mlx_limit("set_cache_limit", _env_mb("HAY_MLX_CACHE_LIMIT_MB"))
        _set_mlx_limit("set_wired_limit", _env_mb("HAY_MLX_WIRED_LIMIT_MB"))
        self._clear_cache_after_prune = _env_flag(
            "HAY_MLX_CLEAR_CACHE_AFTER_PRUNE", True
        )
        self._impl = MLXSwePrunerBackend(
            model_name=model_dir or _resolve_model_dir(), repair=repair
        )
        self._threshold = float(os.environ.get("HAY_THRESHOLD", "0.5"))
        self._max_length = int(os.environ.get("HAY_MAX_LENGTH", "4096"))

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

    def evict(self) -> None:
        self._impl.evict()
