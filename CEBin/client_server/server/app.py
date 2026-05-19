#!/usr/bin/env python3
import argparse
from typing import Any, Dict, List, Literal, Optional

import uvicorn
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from cebin_inference import CEBinInference, FunctionInput, ModelPaths


class EmbedRequest(BaseModel):
    functions: List[FunctionInput] = Field(..., min_length=1)
    encoder: Literal["query", "key"] = "query"
    max_length: int = 1024
    pad_to_multiple_of: int = 8


class EmbedResponse(BaseModel):
    embeddings: List[List[float]]


class ComparePair(BaseModel):
    left: FunctionInput
    right: FunctionInput


class CompareRequest(BaseModel):
    pairs: List[ComparePair] = Field(..., min_length=1)
    max_length: int = 1024
    pad_to_multiple_of: int = 8


class CompareResponse(BaseModel):
    scores: List[float]


def create_app(engine: CEBinInference) -> FastAPI:
    app = FastAPI(title="CEBin inference server", version="1.1")

    @app.get("/v1/health")
    def health() -> Dict[str, str]:
        return {
            "status": "ok",
            "device": str(engine.device),
            "tokenizer": engine.tokenizer_path,
        }

    @app.post("/v1/embed", response_model=EmbedResponse)
    def embed(req: EmbedRequest) -> EmbedResponse:
        try:
            embeddings = engine.embed(
                req.functions,
                encoder=req.encoder,
                max_length=req.max_length,
                pad_to_multiple_of=req.pad_to_multiple_of,
            )
            return EmbedResponse(embeddings=embeddings)
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/v1/compare", response_model=CompareResponse)
    def compare(req: CompareRequest) -> CompareResponse:
        try:
            pairs = [{"left": pair.left, "right": pair.right} for pair in req.pairs]
            scores = engine.compare(
                pairs,
                max_length=req.max_length,
                pad_to_multiple_of=req.pad_to_multiple_of,
            )
            return CompareResponse(scores=scores)
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    return app


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the CEBin GPU inference server.")
    parser.add_argument("--cebin-root", required=True, help="Path to the CEBin repository root.")
    parser.add_argument("--embedding-model", required=True, help="Path to CEBin-Embedding-Cisco.bin.")
    parser.add_argument("--comparison-model", help="Path to CEBin-Comparison-Cisco.bin.")
    parser.add_argument("--tokenizer", help="Path to cebin-tokenizer. Default: <cebin-root>/cebin-tokenizer.")
    parser.add_argument("--device", default="auto", help="auto, cuda:0, cuda:1, mps, or cpu.")
    parser.add_argument("--dtype", default="auto", choices=["auto", "float16", "bfloat16", "float32", "none"])
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--max-length", type=int, default=1024)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    engine = CEBinInference(
        ModelPaths(
            cebin_root=args.cebin_root,
            embedding_model=args.embedding_model,
            comparison_model=args.comparison_model,
            tokenizer=args.tokenizer,
        ),
        device=args.device,
        dtype=args.dtype,
        max_length=args.max_length,
    )
    app = create_app(engine)
    uvicorn.run(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
