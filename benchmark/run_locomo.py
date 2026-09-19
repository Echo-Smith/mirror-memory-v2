"""Run LoCoMo benchmark with Mirror Memory + DeepSeek.

Usage:
    python -m benchmark.run_locomo --project-name mirror-test --max-questions 5
    python -m benchmark.run_locomo --project-name mirror-full
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

# Add parent dirs to path
sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from benchmark.mirror_client import MirrorMemoryClient


async def run_locomo_benchmark(
    project_name: str,
    db_url: str,
    answerer_model: str,
    judge_model: str,
    api_key: str,
    base_url: str,
    max_questions: int | None,
    max_conversations: int,
    output_dir: str,
):
    """Run LoCoMo benchmark with Mirror Memory backend."""
    from benchmarks.common.llm_client import LLMClient
    from benchmarks.common.metrics import compute_overall_metrics
    from benchmarks.common.schema import UnifiedResult
    from benchmarks.locomo.run import (
        download_dataset,
        load_dataset,
        get_sorted_sessions,
        session_to_chunks,
        locomo_date_to_epoch,
        parse_locomo_date,
        compute_locomo_metrics,
        display_results,
    )
    from benchmarks.locomo.prompts import (
        CATEGORY_NAMES,
        JUDGE_SYSTEM_PROMPT,
        get_answer_generation_prompt,
        get_judge_prompt,
        preprocess_answer,
    )

    run_id = uuid.uuid4().hex[:8]
    output_path = os.path.join(output_dir, f"predicted_{project_name}")
    os.makedirs(output_path, exist_ok=True)

    print(f"LoCoMo Benchmark with Mirror Memory")
    print(f"  Project: {project_name}, Run: {run_id}")
    print(f"  Answerer: {answerer_model}")
    print(f"  Judge: {judge_model}")
    print(f"  DB: {db_url}")

    # Download dataset
    import logging
    bench_logger = logging.getLogger("benchmark")
    bench_logger.setLevel(logging.INFO)
    if not bench_logger.handlers:
        bench_logger.addHandler(logging.StreamHandler())
    dataset_path = download_dataset("datasets/locomo", bench_logger)
    dataset = load_dataset(dataset_path)

    # Init clients
    mirror = MirrorMemoryClient(db_url=db_url, app_id="locomo_bench")
    answerer = LLMClient(
        model=answerer_model,
        provider="openai",
        api_key=api_key,
        base_url=base_url,
        rpm=30,
    )
    judge = LLMClient(
        model=judge_model,
        provider="openai",
        api_key=api_key,
        base_url=base_url,
        rpm=30,
    )

    all_evaluations = []
    cutoffs = [10, 20, 50]

    async with mirror:
        for conv_idx in range(min(max_conversations, len(dataset))):
            entry = dataset[conv_idx]
            conversation = entry["conversation"]
            speaker_a = conversation["speaker_a"]
            speaker_b = conversation["speaker_b"]
            user_id = f"locomo_{conv_idx}_{run_id}"

            print(f"\n=== Conversation {conv_idx}: {speaker_a} & {speaker_b} ===")

            # Ingest
            sorted_sessions = get_sorted_sessions(conversation)
            total_chunks = 0
            for session_key, date_str, turns in sorted_sessions:
                chunks = session_to_chunks(turns, speaker_a, speaker_b)
                session_epoch = locomo_date_to_epoch(date_str)

                for chunk_idx, messages in enumerate(chunks):
                    if any(not msg.get("content", "").strip() for msg in messages):
                        continue
                    await mirror.add(messages, user_id, timestamp=session_epoch)
                    total_chunks += 1

            print(f"  Ingested {total_chunks} chunks")

            # Get reference date
            ref_date_human = sorted_sessions[-1][1] if sorted_sessions else None

            # Process questions
            questions = entry.get("qa", entry.get("qa_pairs", []))
            categories = [1, 2, 3, 4]
            conv_questions = [
                (qi, qa) for qi, qa in enumerate(questions)
                if qa.get("category") in categories
            ]
            if max_questions:
                conv_questions = conv_questions[:max_questions]

            for qi, qa in conv_questions:
                question = qa["question"]
                category = qa["category"]
                answer = str(qa["answer"])
                qid = f"conv{conv_idx}_q{qi}"

                # Search
                start = time.monotonic()
                search_results = await mirror.search(question, user_id, top_k=200)
                latency_ms = (time.monotonic() - start) * 1000

                # Answer
                gen_prompt = get_answer_generation_prompt(
                    question, search_results[:50], reference_date=ref_date_human,
                )
                generated = await answerer.generate(system="", user=gen_prompt)
                if "ANSWER:" in generated:
                    generated = generated.rsplit("ANSWER:", 1)[-1].strip()

                # Judge
                processed_answer = preprocess_answer(category, answer)
                judge_prompt = get_judge_prompt(category, question, processed_answer, generated)
                raw_judge = await judge.generate_structured(
                    system=JUDGE_SYSTEM_PROMPT, user=judge_prompt,
                )

                correct = False
                if isinstance(raw_judge, dict):
                    label_val = raw_judge.get("label", "").upper()
                    correct = label_val == "CORRECT"

                cat_name = CATEGORY_NAMES.get(category, "unknown")
                score = 1.0 if correct else 0.0

                result = {
                    "question_id": qid,
                    "conversation_idx": conv_idx,
                    "category": category,
                    "category_name": cat_name,
                    "question": question,
                    "ground_truth_answer": answer,
                    "generated_answer": generated,
                    "judgment": "CORRECT" if correct else "WRONG",
                    "score": score,
                    "search_latency_ms": round(latency_ms, 1),
                    "num_results": len(search_results),
                }

                all_evaluations.append(result)

                status = "✓" if correct else "✗"
                print(f"  {status} [{cat_name}] {question[:60]}...")

                # Save per-question
                result_path = os.path.join(output_path, f"{qid}.json")
                Path(result_path).write_text(json.dumps(result, indent=2, ensure_ascii=False))

    # Compute metrics
    if all_evaluations:
        metrics = compute_locomo_metrics(all_evaluations, cutoffs)
        display_results(metrics, cutoffs)

        # Save unified result
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        unified_path = os.path.join(output_dir, f"locomo_mirror_{timestamp}.json")
        Path(unified_path).write_text(json.dumps({
            "metadata": {
                "benchmark": "locomo",
                "project_name": project_name,
                "run_id": run_id,
                "backend": "mirror_memory",
                "answerer_model": answerer_model,
                "judge_model": judge_model,
                "total_questions": len(all_evaluations),
            },
            "metrics_by_cutoff": metrics,
            "evaluations": all_evaluations,
        }, indent=2, ensure_ascii=False, default=str))
        print(f"\nResults saved to: {unified_path}")

    print(f"\nTotal questions: {len(all_evaluations)}")


def main():
    parser = argparse.ArgumentParser(description="Run LoCoMo with Mirror Memory")
    parser.add_argument("--project-name", default="mirror-locomo")
    parser.add_argument("--db-url", default="sqlite:///./benchmark_mirror.db")
    parser.add_argument("--answerer-model", default="deepseek-chat")
    parser.add_argument("--judge-model", default="deepseek-chat")
    parser.add_argument("--api-key", default=None)
    parser.add_argument("--base-url", default="https://api.deepseek.com/v1")
    parser.add_argument("--max-questions", type=int, default=5)
    parser.add_argument("--max-conversations", type=int, default=1)
    parser.add_argument("--output-dir", default="results/locomo")
    args = parser.parse_args()

    api_key = args.api_key or os.getenv("DEEPSEEK_API_KEY")
    if not api_key:
        print("Error: --api-key or DEEPSEEK_API_KEY required")
        sys.exit(1)

    asyncio.run(run_locomo_benchmark(
        project_name=args.project_name,
        db_url=args.db_url,
        answerer_model=args.answerer_model,
        judge_model=args.judge_model,
        api_key=api_key,
        base_url=args.base_url,
        max_questions=args.max_questions,
        max_conversations=args.max_conversations,
        output_dir=args.output_dir,
    ))


if __name__ == "__main__":
    main()