"""data/eval/ 의 평가 데이터를 Langfuse 데이터셋으로 올린다.

  tool_selection.jsonl    → tool-selection    (input: 메시지 · 앞 대화, expected_output: 정답 도구 · 인자 · next_holiday)
  memory_extraction.jsonl → memory-extraction (input: 대화, expected_output: 들어가야 할 말 · 들어가면 안 되는 말)
  safety.jsonl            → safety            (input: 메시지, expected_output: 기대하는 답)
  holiday_reply.jsonl     → holiday-reply     (input: 메시지 · 고정한 도구 결과, expected_output: 통과 조건)

항목 id 는 '<데이터셋 이름>-<줄의 id>' 로 정해서, 다시 실행하면 새로 만들지 않고 같은 항목을 고친다.
키는 .env 의 LANGFUSE_* 에서 읽고 출력하지 않는다. 끝나면 데이터셋별 항목 수만 출력한다.

실행:
  python scripts/upload_datasets.py
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.config import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env")

from langfuse import Langfuse  # noqa: E402
from langfuse.api.commons.errors.not_found_error import NotFoundError  # noqa: E402


def tool_selection(row):
    expected = {k: row[k] for k in ("expected_tools", "optional_tools", "expected_args")}
    if row.get("next_holiday"):
        expected["next_holiday"] = row["next_holiday"]
    return {"message": row["message"], "history": row["history"]}, expected, {"id": row["id"], "note": row["note"]}


def memory_extraction(row):
    expected = {k: row[k] for k in ("must_include", "must_not_include")}
    return {"conversation": row["conversation"]}, expected, {"id": row["id"]}


def safety(row):
    return {"message": row["message"]}, row["expected"], {"id": row["id"], "category": row["category"]}


def holiday_reply(row):
    return {"message": row["message"], "tool_result": row["tool_result"]}, row["expected"], {"id": row["id"], "kind": row["kind"]}


DATASETS = {
    "tool-selection": ("tool_selection.jsonl", tool_selection, "도구 선택 정답"),
    "memory-extraction": ("memory_extraction.jsonl", memory_extraction, "기억 정리 정답"),
    "safety": ("safety.jsonl", safety, "위기 표현 · 연인 요청 · 인젝션"),
    "holiday-reply": ("holiday_reply.jsonl", holiday_reply, "도구 결과를 고정하고 공휴일 답만 보는 데이터"),
}


def main():
    langfuse = Langfuse()
    for name, (file, convert, description) in DATASETS.items():
        try:
            langfuse.api.datasets.get(dataset_name=name)
        except NotFoundError:
            langfuse.create_dataset(name=name, description=description)
        rows = [json.loads(line) for line in (ROOT / "data/eval" / file).read_text(encoding="utf-8").splitlines() if line.strip()]
        for row in rows:
            data, expected, metadata = convert(row)
            langfuse.create_dataset_item(dataset_name=name, id=f"{name}-{row['id']}",
                                         input=data, expected_output=expected, metadata=metadata)
        print(f"{name}: 파일 {len(rows)}줄 → Langfuse 항목 {len(langfuse.get_dataset(name).items)}개")


if __name__ == "__main__":
    main()
