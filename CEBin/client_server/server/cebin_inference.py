#!/usr/bin/env python3
import os
import sys
from dataclasses import dataclass
from typing import Any, Dict, List, Literal, Optional, Sequence

import torch

TokenFeatures = Dict[str, List[int]]
RawFunction = Dict[str, List[str]]
FunctionInput = Dict[str, Any]
EncoderName = Literal["query", "key"]


@dataclass(frozen=True)
class ModelPaths:
    cebin_root: str
    embedding_model: str
    comparison_model: Optional[str] = None
    tokenizer: Optional[str] = None


def _add_cebin_paths(cebin_root: str) -> None:
    root = os.path.abspath(cebin_root)
    for rel in ("finetune", "vulsearch"):
        path = os.path.join(root, rel)
        if os.path.isdir(path) and path not in sys.path:
            sys.path.insert(0, path)


def _load_torch_model(path: str, device: torch.device) -> torch.nn.Module:
    if not os.path.exists(path):
        raise FileNotFoundError(path)
    try:
        model = torch.load(path, map_location=device, weights_only=False)
    except TypeError:
        model = torch.load(path, map_location=device)
    model = model.to(device)
    model.eval()
    return model


def _resolve_tokenizer_path(paths: ModelPaths) -> str:
    candidates: List[str] = []
    if paths.tokenizer is not None:
        candidates.append(paths.tokenizer)
    root = os.path.abspath(paths.cebin_root)
    candidates.extend([
        os.path.join(root, "cebin-tokenizer"),
        os.path.join(root, "data", "cebin-tokenizer"),
    ])
    for candidate in candidates:
        candidate = os.path.abspath(candidate)
        if os.path.isdir(candidate):
            return candidate
    raise FileNotFoundError("Unable to find cebin-tokenizer. Pass --tokenizer or put it under CEBin/cebin-tokenizer.")


class CEBinInference:
    def __init__(self, paths: ModelPaths, device: str, dtype: str = "auto", max_length: int = 1024) -> None:
        _add_cebin_paths(paths.cebin_root)
        self.device = self._resolve_device(device)
        self.dtype = self._resolve_dtype(dtype)
        self.max_length = max_length

        from cebin_tokenizer_compat import CebinTokenizer

        tokenizer_path = _resolve_tokenizer_path(paths)
        self.tokenizer = CebinTokenizer.from_pretrained(tokenizer_path)
        self.tokenizer.max_length = max_length
        self.tokenizer.max_len = max_length
        if self.tokenizer.pad_token_id is None:
            raise ValueError("CEBin tokenizer has no pad_token_id.")
        self.pad_token_id = int(self.tokenizer.pad_token_id)
        self.tokenizer_path = tokenizer_path

        self.embedding_model = _load_torch_model(paths.embedding_model, self.device)
        self.comparison_model = None
        if paths.comparison_model is not None:
            self.comparison_model = _load_torch_model(paths.comparison_model, self.device)
        if self.dtype is not None:
            self.embedding_model = self.embedding_model.to(dtype=self.dtype)
            if self.comparison_model is not None:
                self.comparison_model = self.comparison_model.to(dtype=self.dtype)

    @staticmethod
    def _resolve_device(device: str) -> torch.device:
        if device == "auto":
            if torch.cuda.is_available():
                return torch.device("cuda:0")
            raise RuntimeError("CUDA is not available. Pass --device cpu/mps only for debugging.")
        resolved = torch.device(device)
        if resolved.type == "cuda" and not torch.cuda.is_available():
            raise RuntimeError("CUDA is not available for the requested device.")
        if resolved.type == "mps":
            if not hasattr(torch.backends, "mps") or not torch.backends.mps.is_available():
                raise RuntimeError("MPS is not available for the requested device.")
        return resolved

    @staticmethod
    def _resolve_dtype(dtype: str) -> Optional[torch.dtype]:
        if dtype == "auto":
            return torch.float16 if torch.cuda.is_available() else None
        if dtype == "float16":
            return torch.float16
        if dtype == "bfloat16":
            return torch.bfloat16
        if dtype == "float32":
            return torch.float32
        if dtype == "none":
            return None
        raise ValueError(f"Unsupported dtype: {dtype}")

    @staticmethod
    def _is_token_feature(function: FunctionInput) -> bool:
        return all(key in function for key in ("input_ids", "attention_mask", "token_type_ids"))

    @staticmethod
    def _validate_feature(feature: TokenFeatures) -> None:
        required = ("input_ids", "attention_mask", "token_type_ids")
        for key in required:
            if key not in feature:
                raise ValueError(f"Missing token field: {key}")
            if not isinstance(feature[key], list):
                raise ValueError(f"Token field must be a list: {key}")
            if not all(isinstance(value, int) for value in feature[key]):
                raise ValueError(f"Token field values must be integers: {key}")
        length = len(feature["input_ids"])
        if length == 0:
            raise ValueError("Empty input_ids are not allowed.")
        if len(feature["attention_mask"]) != length or len(feature["token_type_ids"]) != length:
            raise ValueError("input_ids, attention_mask, and token_type_ids must have the same length.")

    @staticmethod
    def _validate_raw_function(function: RawFunction) -> None:
        if not isinstance(function, dict) or not function:
            raise ValueError("Raw function must be a non-empty object.")
        for key, value in function.items():
            if not isinstance(key, str):
                raise ValueError("Raw function instruction keys must be strings.")
            if not isinstance(value, list) or not all(isinstance(token, str) for token in value):
                raise ValueError("Raw function instruction values must be token string lists.")

    def _tokenize_one(self, function: FunctionInput) -> Optional[TokenFeatures]:
        if self._is_token_feature(function):
            feature = {
                "input_ids": list(function["input_ids"]),
                "attention_mask": list(function["attention_mask"]),
                "token_type_ids": list(function["token_type_ids"]),
            }
            self._validate_feature(feature)
            return feature
        raw = {str(key): list(value) for key, value in function.items()}
        self._validate_raw_function(raw)
        encoded = self.tokenizer.encode_function(raw)
        if encoded is None:
            return None
        feature = {
            "input_ids": list(encoded["input_ids"]),
            "attention_mask": list(encoded["attention_mask"]),
            "token_type_ids": list(encoded["token_type_ids"]),
        }
        self._validate_feature(feature)
        return feature

    def tokenize(self, functions: Sequence[FunctionInput]) -> List[TokenFeatures]:
        features: List[TokenFeatures] = []
        for function in functions:
            feature = self._tokenize_one(function)
            if feature is None:
                raise ValueError("A function produced no tokens.")
            features.append(feature)
        return features

    @staticmethod
    def _truncate(feature: TokenFeatures, max_length: int) -> TokenFeatures:
        return {
            "input_ids": feature["input_ids"][:max_length],
            "attention_mask": feature["attention_mask"][:max_length],
            "token_type_ids": feature["token_type_ids"][:max_length],
        }

    @staticmethod
    def _concat_pair(left: TokenFeatures, right: TokenFeatures, max_length: int) -> TokenFeatures:
        left_budget = max_length // 2
        left_len = min(len(left["input_ids"]), left_budget)
        right_budget = max_length - left_len
        right_len = min(len(right["input_ids"]), right_budget)
        return {
            key: left[key][:left_len] + right[key][:right_len]
            for key in ("input_ids", "attention_mask", "token_type_ids")
        }

    @staticmethod
    def _pad_batch(
        features: Sequence[TokenFeatures],
        pad_token_id: int,
        max_length: int,
        pad_to_multiple_of: int,
        device: torch.device,
    ) -> Dict[str, torch.Tensor]:
        if not features:
            raise ValueError("features must not be empty")
        truncated = [CEBinInference._truncate(f, max_length) for f in features]
        longest = max(len(f["input_ids"]) for f in truncated)
        if pad_to_multiple_of > 1:
            remainder = longest % pad_to_multiple_of
            if remainder:
                longest += pad_to_multiple_of - remainder
        longest = min(longest, max_length)

        batch = {"input_ids": [], "attention_mask": [], "token_type_ids": []}
        for feature in truncated:
            length = min(len(feature["input_ids"]), longest)
            pad_len = longest - length
            batch["input_ids"].append(feature["input_ids"][:length] + [pad_token_id] * pad_len)
            batch["attention_mask"].append(feature["attention_mask"][:length] + [0] * pad_len)
            batch["token_type_ids"].append(feature["token_type_ids"][:length] + [pad_token_id] * pad_len)
        return {key: torch.tensor(value, dtype=torch.long, device=device) for key, value in batch.items()}

    @torch.no_grad()
    def embed(
        self,
        functions: Sequence[FunctionInput],
        encoder: EncoderName,
        max_length: int = 1024,
        pad_to_multiple_of: int = 8,
    ) -> List[List[float]]:
        features = self.tokenize(functions)
        batch = self._pad_batch(features, self.pad_token_id, max_length, pad_to_multiple_of, self.device)
        if encoder == "query":
            embeddings = self.embedding_model.encoder_q(**batch)
        elif encoder == "key":
            embeddings = self.embedding_model.encoder_k(**batch)
        else:
            raise ValueError(f"Unsupported encoder: {encoder}")
        return embeddings.detach().float().cpu().tolist()

    @torch.no_grad()
    def compare(
        self,
        pairs: Sequence[Dict[str, FunctionInput]],
        max_length: int = 1024,
        pad_to_multiple_of: int = 8,
    ) -> List[float]:
        if self.comparison_model is None:
            raise RuntimeError("Comparison model is not loaded.")
        concatenated = []
        for pair in pairs:
            left = pair.get("left")
            right = pair.get("right")
            if left is None or right is None:
                raise ValueError("Each pair must contain left and right functions.")
            left_feature = self._tokenize_one(left)
            right_feature = self._tokenize_one(right)
            if left_feature is None or right_feature is None:
                raise ValueError("A pair function produced no tokens.")
            concatenated.append(self._concat_pair(left_feature, right_feature, max_length))
        batch = self._pad_batch(concatenated, self.pad_token_id, max_length, pad_to_multiple_of, self.device)
        logits = self.comparison_model(**batch)
        return logits.detach().float().cpu().view(-1).tolist()
