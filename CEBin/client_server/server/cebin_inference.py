#!/usr/bin/env python3
import argparse
import os
import sys
from dataclasses import dataclass
from typing import Dict, List, Literal, Optional, Sequence

import torch

TokenFeatures = Dict[str, List[int]]
EncoderName = Literal["query", "key"]


@dataclass(frozen=True)
class ModelPaths:
    cebin_root: str
    embedding_model: str
    comparison_model: Optional[str] = None


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


class CEBinInference:
    def __init__(self, paths: ModelPaths, device: str, dtype: str = "auto") -> None:
        _add_cebin_paths(paths.cebin_root)
        self.device = self._resolve_device(device)
        self.dtype = self._resolve_dtype(dtype)
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
            raise RuntimeError("CUDA is not available. Pass --device cpu only for debugging.")
        resolved = torch.device(device)
        if resolved.type == "cuda" and not torch.cuda.is_available():
            raise RuntimeError("CUDA is not available for the requested device.")
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
    def _validate_feature(feature: TokenFeatures) -> None:
        required = ("input_ids", "attention_mask", "token_type_ids")
        for key in required:
            if key not in feature:
                raise ValueError(f"Missing token field: {key}")
            if not isinstance(feature[key], list):
                raise ValueError(f"Token field must be a list: {key}")
        length = len(feature["input_ids"])
        if length == 0:
            raise ValueError("Empty input_ids are not allowed.")
        if len(feature["attention_mask"]) != length or len(feature["token_type_ids"]) != length:
            raise ValueError("input_ids, attention_mask, and token_type_ids must have the same length.")

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
            length = len(feature["input_ids"])
            pad_len = longest - length
            batch["input_ids"].append(feature["input_ids"] + [pad_token_id] * pad_len)
            batch["attention_mask"].append(feature["attention_mask"] + [0] * pad_len)
            batch["token_type_ids"].append(feature["token_type_ids"] + [pad_token_id] * pad_len)
        return {key: torch.tensor(value, dtype=torch.long, device=device) for key, value in batch.items()}

    @torch.no_grad()
    def embed(
        self,
        features: Sequence[TokenFeatures],
        encoder: EncoderName,
        pad_token_id: int,
        max_length: int = 1024,
        pad_to_multiple_of: int = 8,
    ) -> List[List[float]]:
        for feature in features:
            self._validate_feature(feature)
        batch = self._pad_batch(features, pad_token_id, max_length, pad_to_multiple_of, self.device)
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
        pairs: Sequence[Dict[str, TokenFeatures]],
        pad_token_id: int,
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
                raise ValueError("Each pair must contain left and right features.")
            self._validate_feature(left)
            self._validate_feature(right)
            concatenated.append(self._concat_pair(left, right, max_length))
        batch = self._pad_batch(concatenated, pad_token_id, max_length, pad_to_multiple_of, self.device)
        logits = self.comparison_model(**batch)
        return logits.detach().float().cpu().view(-1).tolist()
