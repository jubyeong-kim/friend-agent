"""A/B 나눠 보내기. AB_TEST(실험 이름)가 있을 때만 켜진다.

사용자를 '실험 이름:사용자 ID' 의 해시로 A 와 B 로 나눈다. 같은 사람은 늘 같은 쪽이고,
실험 이름을 바꾸면 사람들이 새로 섞인다(앞 실험에서 B 였던 사람만 늘 B 가 되지 않게).
B 비율은 AB_B_PERCENT(0 ~ 100, 기본 50). A 는 prod-a, B 는 prod-b 라벨의 프롬프트를 쓴다.
"""
import hashlib
import os

LABELS = {"a": "prod-a", "b": "prod-b"}
B_PERCENT = 50


def experiment():
    """켜져 있는 실험 이름. 꺼져 있으면 빈 문자열."""
    return os.environ.get("AB_TEST", "").strip()


def variant(user_id):
    """이 사용자가 떨어지는 쪽('a' 또는 'b'). 실험이 꺼져 있거나 사용자 ID 가 없으면 None."""
    name = experiment()
    if not name or not user_id:
        return None
    percent = int(os.environ.get("AB_B_PERCENT", "").strip() or B_PERCENT)
    bucket = int(hashlib.sha256(f"{name}:{user_id}".encode()).hexdigest()[:8], 16) % 100  # 0 ~ 99
    return "b" if bucket < percent else "a"
