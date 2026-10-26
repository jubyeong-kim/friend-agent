"""약속 카드 그림. 약속 하나를 PNG 카드로 그려 저장하거나 보낼 수 있게 한다.

그림은 Python 라이브러리(Pillow)로 그리지만, 한글을 그리려면 운영체제에 한글 글꼴 파일이 있어야 한다.
노트북에는 보통 들어 있고(Windows 맑은 고딕), 서버에서는 실행 환경(이미지)에 들어 있어야 한다.
한글이 없는 글꼴로 그리면 오류 없이 네모(□)로 나오므로, 한글 글꼴을 못 찾으면 그리지 않고 오류로 알린다.
"""
import io
import os

from app.logs import log

# 한글 글꼴을 찾는 곳(앞에서부터). CARD_FONT 환경변수로 글꼴 파일을 직접 정할 수도 있다.
FONT_PATHS = [
    r"C:\Windows\Fonts\malgun.ttf",                            # Windows 맑은 고딕
    "/System/Library/Fonts/AppleSDGothicNeo.ttc",               # macOS
    "/usr/share/fonts/truetype/nanum/NanumGothic.ttf",          # 리눅스 나눔고딕
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",   # 리눅스 Noto Sans CJK
]

WIDTH, HEIGHT, PAD = 800, 460, 56
COLORS = {"bg": "#f7f8fb", "card": "#ffffff", "line": "#e6e8f0", "ink": "#0f1222", "ink2": "#4a5068",
          "ink3": "#7d849b", "rem": "#e5484d", "rem_soft": "#fff0f0"}


class CardError(Exception):
    pass


def find_font():
    """한글 글꼴 파일 경로. 못 찾으면 None."""
    chosen = os.environ.get("CARD_FONT", "").strip()
    for path in ([chosen] if chosen else []) + FONT_PATHS:
        if os.path.exists(path):
            return path
    return None


def wrap(text, font, width):
    """줄 너비를 넘으면 마지막 띄어쓰기에서 줄을 바꾼다. 띄어쓰기 없이 긴 말은 글자 단위로 자른다."""
    lines, line = [], ""
    for ch in text:
        if font.getlength(line + ch) <= width or not line:
            line += ch
            continue
        cut = line.rfind(" ")
        if cut > 0:
            lines.append(line[:cut])
            line = line[cut + 1:] + ch
        else:
            lines.append(line)
            line = ch.lstrip()
    return lines + [line] if line.strip() else lines


def draw_reminder(text, due_label):
    """약속 내용과 '10월 30일(목) 19:00' 같은 시각으로 카드 PNG 를 만든다."""
    from PIL import Image, ImageDraw, ImageFont

    path = find_font()
    if not path:
        log("ERROR", "한글 글꼴을 찾을 수 없습니다. 실행 환경에 한글 글꼴이 설치되어 있지 않습니다.")
        raise CardError("그림에 쓸 한글 글꼴이 서버에 없습니다.")

    def font(size):
        return ImageFont.truetype(path, size)

    image = Image.new("RGB", (WIDTH, HEIGHT), COLORS["bg"])
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((24, 24, WIDTH - 24, HEIGHT - 24), radius=28, fill=COLORS["card"], outline=COLORS["line"], width=2)

    # 위: 「약속」 표시와 시각
    tag = font(24)
    tag_w = draw.textlength("약속", font=tag)
    draw.rounded_rectangle((PAD, PAD, PAD + tag_w + 32, PAD + 44), radius=22, fill=COLORS["rem_soft"])
    draw.text((PAD + 16, PAD + 22), "약속", font=tag, fill=COLORS["rem"], anchor="lm")
    draw.text((PAD, PAD + 80), due_label, font=font(44), fill=COLORS["ink"])

    # 가운데: 약속 내용(최대 세 줄)
    body = font(32)
    lines = wrap(" ".join(text.split()), body, WIDTH - PAD * 2)
    if len(lines) > 3:
        lines = lines[:3]
        lines[-1] = lines[-1][:-1] + "…"
    y = PAD + 150
    for line in lines:
        draw.text((PAD, y), line, font=body, fill=COLORS["ink2"])
        y += 44

    draw.text((PAD, HEIGHT - PAD - 8), "AI 친구가 시간이 되면 알려 줄게요", font=font(20), fill=COLORS["ink3"], anchor="ls")

    out = io.BytesIO()
    image.save(out, format="PNG")
    log("INFO", "약속 카드 그림", font=os.path.basename(path), lines=len(lines), bytes=out.tell())
    return out.getvalue()
