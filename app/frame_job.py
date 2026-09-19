"""Daytona 샌드박스 안에서 실행되는 카메라 프레임 점검 스크립트.

시각장애인은 자기가 찍은 화면이 어두운지·흔들렸는지·아까와 같은 장면인지 볼 수 없다.
그래서 비전 AI 에 보내기 전에 ① 밝기 ② 선명도 ③ 직전 프레임 대비 변화량을 잰다.
변화가 없으면 말하지 않는다 — 실시간 안내의 품질은 '침묵'에서 나온다.

사용: python frame_job.py <frame.jpg> <prev.jpg>   (결과는 표준출력 한 줄: FRAME_OK {json})
"""
import json
import os
import shutil
import sys

from PIL import Image, ImageChops, ImageFilter, ImageStat

DARK_LUMA = 40            # 평균 밝기(0~255)가 이보다 낮으면 렌즈가 가려졌거나 너무 어둡다
BRIGHT_LUMA = 235
BLUR_EDGE_STDDEV = 6.0    # 윤곽선 필터 표준편차가 이보다 낮으면 흔들린 화면
CHANGE_THRESHOLD = 10.0   # 64x64 흑백 평균 차이가 이보다 작으면 같은 장면
COMPARE_SIZE = (64, 64)
SHARPNESS_WIDTH = 320
MAX_SIDE = 4096

HINT_DARK = "화면이 너무 어둡습니다. 렌즈가 가려지지 않았는지 확인하고 밝은 쪽으로 향해 주세요."
HINT_BRIGHT = "빛이 너무 강합니다. 조명이나 창을 등지고 다시 비춰 주세요."
HINT_BLUR = "화면이 흔들렸습니다. 잠시 멈추고 다시 비춰 주세요."


def analyze(frame_path: str, prev_path: str | None = None) -> dict:
    with Image.open(frame_path) as im:
        if max(im.size) > MAX_SIDE:
            raise ValueError("이미지가 너무 큽니다.")
        gray = im.convert("L")
    luma = ImageStat.Stat(gray).mean[0]
    small = gray.resize((SHARPNESS_WIDTH, max(1, SHARPNESS_WIDTH * gray.height // gray.width)))
    edges = small.filter(ImageFilter.FIND_EDGES)
    edges = edges.crop((2, 2, edges.width - 2, edges.height - 2))  # 필터가 테두리에 만드는 가짜 윤곽선 제외
    sharpness = ImageStat.Stat(edges).stddev[0]

    change = None
    if prev_path and os.path.exists(prev_path):
        with Image.open(prev_path) as prev:
            diff = ImageChops.difference(gray.resize(COMPARE_SIZE), prev.convert("L").resize(COMPARE_SIZE))
        change = ImageStat.Stat(diff).mean[0]

    hints = []
    if luma < DARK_LUMA:
        hints.append(HINT_DARK)
    elif luma > BRIGHT_LUMA:
        hints.append(HINT_BRIGHT)
    elif sharpness < BLUR_EDGE_STDDEV:
        hints.append(HINT_BLUR)

    return {
        "luma": round(luma, 1),
        "sharpness": round(sharpness, 1),
        "change": None if change is None else round(change, 1),
        "changed": change is None or change >= CHANGE_THRESHOLD,
        "usable": luma >= DARK_LUMA,
        "hints": hints,
    }


def main(frame_path: str, prev_path: str) -> None:
    result = analyze(frame_path, prev_path)
    shutil.copyfile(frame_path, prev_path)  # 다음 호출의 비교 기준
    print("FRAME_OK " + json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    try:
        main(sys.argv[1], sys.argv[2])
    except Exception as exc:
        print("FRAME_FAIL %s" % exc)
        sys.exit(1)
