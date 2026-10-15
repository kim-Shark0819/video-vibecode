"""결과물을 PDF 파일로 만든다. 콘티 기획표 · 대본 · 로그라인.

## 왜 서버에서 만드는가

브라우저 인쇄(window.print)로도 종이를 얻을 수 있었고 실제로 그 경로가 있었다. 그런데
사용자가 받는 것은 파일이 아니라 **인쇄 대화상자**다. "PDF로 저장"을 직접 골라야 하고,
브라우저마다 그 항목의 이름과 위치가 다르다. 더 나쁜 것은 결과물 자체가 오염된다는
점이다. 참고로 받은 `콘티_러프_1.pdf` 를 열어 보면 그 흔적이 그대로 남아 있다.

    26. 8. 25. 오후 1:53            Toon Motion AI          ← 브라우저 머리글
    https://ai-pj.testlink.click/images                2/4  ← 브라우저 바닥글

회의 자료로 돌리는 문서에 내부 URL 과 접속 시각이 찍혀 나간다. 여백·배경 인쇄 여부도
사용자 설정에 딸려 가므로 **같은 콘티가 사람마다 다르게 나온다.**

서버에서 만들면 파일 하나가 그대로 내려오고, 누가 눌러도 같은 결과다.

## 양식은 참고 문서를 따른다

컬럼 구성(Cut / Video / Context / Audio / Time), 세로 A4, 컷 번호 아래 숏 크기,
Audio 칸의 대사·음향·메모 라벨까지 `콘티_러프_1.pdf` 와 화면의 기획표(.sb-sheet)를
따른다. 세 곳이 갈라지면 같은 콘티가 화면과 종이에서 다르게 보인다.

## 왜 CID 폰트인가

한국어를 넣으려면 폰트가 필요하다. 선택지는 두 개였다.

  1. Pretendard(이 프로젝트가 이미 가진 폰트)를 변환해 임베드
  2. reportlab 내장 CID 폰트(HYSMyeongJo-Medium / HYGothic-Medium)

2번을 쓴다. 1번은 `frontend/public/fonts` 의 woff2 를 그대로 쓸 수 없어(reportlab 은
TTF/OTF) 변환한 파일을 앱 이미지에 새로 넣어야 하고, 웹폰트 재배포 조건을 따로 확인해야
한다. CID 폰트는 파일이 필요 없고 Acrobat 표준에 들어 있어 어디서나 열린다. 화면과
글꼴이 완전히 같지는 않지만, 이 문서의 목적은 브랜드 일치가 아니라 읽히는 것이다.

## 이미지가 없는 컷을 빈 칸으로 두지 않는다

콘티는 컷마다 모델을 한 번씩 부르는 비동기 작업이라, 내려받는 시점에 아직 안 그려진 컷이
있을 수 있다. 빈 칸을 두면 "안 그려진 것"과 "그리는 중"과 "실패"를 구별할 수 없다.
그 자리에 상태를 적는다. 화면(shotPlaceholder)이 같은 판단을 한다.
"""

import io
import logging

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.cidfonts import UnicodeCIDFont
from reportlab.platypus import (
    Image,
    KeepTogether,
    PageBreak,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

logger = logging.getLogger("toon-change-ai-pj-app.pdf")

SERIF = "HYSMyeongJo-Medium"
SANS = "HYGothic-Medium"
_fonts_ready = False


def _ensure_fonts() -> None:
    """CID 폰트를 한 번만 등록한다. 매 요청 반복할 이유가 없다."""
    global _fonts_ready
    if _fonts_ready:
        return
    for name in (SERIF, SANS):
        pdfmetrics.registerFont(UnicodeCIDFont(name))
    _fonts_ready = True


# 색은 화면(styles.css)과 같은 값을 쓴다. 두 곳이 갈리면 같은 콘티가 다르게 보인다.
INK = colors.HexColor("#2c2c33")
LINE = colors.HexColor("#d8d8de")
HEAD_LINE = colors.HexColor("#2c2c33")
DIM = colors.HexColor("#6f6f78")
ACCENT = colors.HexColor("#5234c1")


def _styles() -> dict:
    _ensure_fonts()
    body = ParagraphStyle("body", fontName=SERIF, fontSize=8.2, leading=12, textColor=INK)
    return {
        "body": body,
        "dim": ParagraphStyle("dim", parent=body, textColor=DIM),
        "cutNo": ParagraphStyle("cutNo", fontName=SANS, fontSize=10, leading=12,
                                textColor=INK, alignment=TA_CENTER),
        "cutShot": ParagraphStyle("cutShot", fontName=SERIF, fontSize=6.4, leading=8.5,
                                  textColor=DIM, alignment=TA_CENTER),
        "time": ParagraphStyle("time", parent=body, alignment=TA_CENTER),
        "frameNote": ParagraphStyle("frameNote", fontName=SERIF, fontSize=7, leading=9.5,
                                    textColor=DIM, alignment=TA_CENTER),
        "th": ParagraphStyle("th", fontName=SANS, fontSize=8, leading=10,
                             textColor=INK, alignment=TA_CENTER),
        "title": ParagraphStyle("title", fontName=SANS, fontSize=13, leading=17, textColor=INK),
        "meta": ParagraphStyle("meta", fontName=SERIF, fontSize=8, leading=11.5, textColor=DIM),
        "sheetHead": ParagraphStyle("sheetHead", fontName=SERIF, fontSize=8.4, leading=12,
                                    textColor=INK),
        # --- 대본 ---
        "slug": ParagraphStyle("slug", fontName=SANS, fontSize=10, leading=14, textColor=INK,
                               spaceBefore=11, spaceAfter=4),
        "action": ParagraphStyle("action", fontName=SERIF, fontSize=9.4, leading=15,
                                 textColor=INK, spaceAfter=5),
        "character": ParagraphStyle("character", fontName=SANS, fontSize=9.2, leading=13,
                                    textColor=INK, leftIndent=38 * mm, spaceBefore=5),
        "line": ParagraphStyle("line", fontName=SERIF, fontSize=9.4, leading=14, textColor=INK,
                               leftIndent=26 * mm, rightIndent=14 * mm, spaceAfter=3),
        "note": ParagraphStyle("note", fontName=SERIF, fontSize=8, leading=11.5, textColor=DIM,
                               spaceAfter=4),
        "hook": ParagraphStyle("hook", fontName=SANS, fontSize=8.8, leading=13, textColor=INK,
                               backColor=colors.HexColor("#f1f1f4"), borderPadding=6,
                               spaceBefore=10),
    }


def _text(value) -> str:
    """Paragraph 에 넣을 수 있게 다듬는다.

    `&`, `<`, `>` 를 escape 한다. Paragraph 는 내용을 작은 마크업으로 해석하므로, 대사에
    부등호가 들어가면(모델이 실제로 쓴다) 문서 생성이 예외로 죽는다. `&` 를 가장 먼저
    바꾼다 — 순서를 바꾸면 이미 넣은 `&lt;` 의 `&` 가 다시 치환돼 `&amp;lt;` 가 된다.

    줄바꿈은 `<br/>` 로 살린다. 지문의 줄 구분이 곧 동작 단위다.
    """
    raw = str(value or "").replace("\r\n", "\n").strip()
    escaped = raw.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return escaped.replace("\n", "<br/>")


def _label(name: str, value) -> str:
    """`음향 형광등 윙윙거리는 소리` 처럼 작은 라벨을 앞에 붙인다. 참고 문서와 같은 형태."""
    return f"<font name='{SANS}' size=6.4 color='#5234c1'>{name}</font> {_text(value)}"


def _footer(canvas, doc) -> None:
    """바닥글: AI 생성 표기와 페이지 번호.

    지시서 0절이 "생성물은 전부 AI 생성 표기"를 요구한다. 화면의 뱃지는 이 파일에
    따라오지 않으므로 여기서 붙인다. 파일이 회의실에서 인쇄되어 돌아다니는 것을 전제한다.

    브라우저 인쇄가 넣던 접속 URL 과 시각은 넣지 않는다. 그것을 없애려고 만든 경로다.
    """
    canvas.saveState()
    canvas.setFont(SERIF, 7.2)
    canvas.setFillColor(DIM)
    canvas.drawString(doc.leftMargin, 9 * mm, "AI 생성 결과물 · 이매지너스")
    canvas.drawRightString(doc.pagesize[0] - doc.rightMargin, 9 * mm, str(canvas.getPageNumber()))
    canvas.restoreState()


def _build(doc: SimpleDocTemplate, story: list, buffer: io.BytesIO) -> bytes:
    doc.build(story, onFirstPage=_footer, onLaterPages=_footer)
    return buffer.getvalue()


def _doc(buffer: io.BytesIO, title: str, subject: str, **margins) -> SimpleDocTemplate:
    return SimpleDocTemplate(
        buffer,
        pagesize=A4,
        title=title,
        author="이매지너스",
        subject=subject,
        # 생성기를 밝힌다. 파일 속성만 보고도 어디서 나온 문서인지 알 수 있어야 한다.
        creator="이매지너스 (Imaginus)",
        **margins,
    )


# --------------------------------------------------------------------------- #
# 콘티 기획표
# --------------------------------------------------------------------------- #
# 세로 A4. 컬럼 폭은 참고 문서(콘티_러프_1.pdf)의 비율을 그대로 옮겼다.
# 합계 177mm = 본문 폭(210 - 좌우 여백 16.5mm x 2).
_COL_CUT = 17 * mm
_COL_VIDEO = 40 * mm
_COL_CONTEXT = 68 * mm
_COL_AUDIO = 37 * mm
_COL_TIME = 15 * mm
_COLS = [_COL_CUT, _COL_VIDEO, _COL_CONTEXT, _COL_AUDIO, _COL_TIME]

# Video 칸 안쪽 그림이 쓸 수 있는 최대 크기. 세로는 16:9 를 기준으로 두되 실제 그림의
# 비율에 맞춰 줄인다 — 비율을 무시하고 늘리면 인물이 찌그러진다.
_FRAME_W = _COL_VIDEO - 6 * mm
_FRAME_H = 30 * mm


def storyboard_pdf(
    title: str,
    cuts: list[dict],
    *,
    meta: str = "",
    sheet_meta: str = "",
    image_loader=None,
) -> bytes:
    """콘티 기획표 PDF.

    `cuts` 는 화면과 같은 규칙으로 합친 컷 목록이다(number/shotLabel/context/dialogue/
    audio/note/seconds/status/error/imageKey). `image_loader(imageKey) -> bytes | None`
    로 그림을 읽는다. 호출부가 S3 접근을 갖고 이 모듈은 갖지 않는다 — 문서 조립과
    저장소 접근을 섞지 않는다.
    """
    style = _styles()
    buffer = io.BytesIO()
    doc = _doc(
        buffer, title or "콘티 기획표", "AI 생성 콘티 기획표",
        leftMargin=16.5 * mm, rightMargin=16.5 * mm,
        topMargin=15 * mm, bottomMargin=15 * mm,
    )

    total_seconds = sum(int(cut.get("seconds") or 0) for cut in cuts)

    # 표를 먼저 조립한다. 머리글의 "그림 N/M컷" 이 **실제로 문서에 들어간 그림 수**여야
    # 하기 때문이다. imageKey 가 있는 컷을 세면 S3 에서 못 읽은 컷과 깨진 파일까지 세어,
    # 그림이 2장 실린 문서에 4장이라고 적힌다. 문서에 박히는 숫자는 사실이어야 한다.
    rows = [[Paragraph(label, style["th"])
             for label in ("Cut", "Video", "Context", "Audio", "Time")]]
    drawn = 0

    for index, cut in enumerate(cuts, start=1):
        number = cut.get("number") or index
        cut_cell = [Paragraph(str(number), style["cutNo"])]
        shot = _text(cut.get("shotLabel") or cut.get("shot") or "")
        if shot:
            cut_cell.append(Paragraph(shot, style["cutShot"]))

        frame, embedded = _frame_cell(cut, style, image_loader)
        drawn += 1 if embedded else 0

        seconds = int(cut.get("seconds") or 0)
        rows.append([
            cut_cell,
            frame,
            Paragraph(_text(cut.get("context")) or "-", style["body"]),
            _audio_cell(cut, style),
            Paragraph(f"{seconds}s" if seconds else "-", style["time"]),
        ])

    story: list = [Paragraph(_text(title or "콘티 기획표"), style["title"])]
    if meta:
        story.append(Paragraph(_text(meta), style["meta"]))
    if sheet_meta:
        story.append(Paragraph(_text(sheet_meta), style["meta"]))
    # 참고 문서의 "컷 12/12 완성" 줄에 해당한다. 몇 컷이 그려졌는지 문서 안에 남는다.
    story.append(Spacer(1, 3 * mm))
    story.append(
        Paragraph(
            f"Title : <b>{_text(title or '-')}</b>　　Cuts : <b>{len(cuts)}컷</b>"
            f"　　Total : <b>{total_seconds}초</b>　　그림 : <b>{drawn}/{len(cuts)}컷</b>",
            style["sheetHead"],
        )
    )
    story.append(Spacer(1, 2.5 * mm))

    table = Table(rows, colWidths=_COLS, repeatRows=1)
    table.setStyle(TableStyle([
        # 머리행은 굵은 아래선으로 가른다. 참고 문서와 같다(칸을 칠하지 않는다).
        ("LINEBELOW", (0, 0), (-1, 0), 1.1, HEAD_LINE),
        ("LINEABOVE", (0, 0), (-1, 0), 0.8, HEAD_LINE),
        ("LINEBELOW", (0, 1), (-1, -1), 0.5, LINE),
        ("LINEAFTER", (0, 0), (-2, -1), 0.5, LINE),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("ALIGN", (1, 1), (1, -1), "CENTER"),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
        ("LEFTPADDING", (0, 0), (-1, -1), 4),
        ("RIGHTPADDING", (0, 0), (-1, -1), 4),
    ]))
    story.append(table)
    return _build(doc, story, buffer)


# 그림을 다시 인코딩할 때 목표 해상도. 표 안의 칸은 최대 40mm 폭이고, 인쇄 품질
# 기준인 300DPI 로 채우려면 (40mm ≒ 113pt) → 113/72*300 ≒ 472px 면 충분하다.
# 여유를 두어 600px 로 잡는다.
_MAX_EDGE_PX = 600
_JPEG_QUALITY = 85


def _prepare_image(data: bytes) -> tuple[bytes, float, float]:
    """넣을 그림을 표 칸 크기에 맞춰 줄이고, 그릴 크기를 함께 돌려준다.

    ## 왜 원본을 그대로 넣지 않는가

    모델이 내놓는 컷은 1024px 이상의 PNG 다. 그것을 40mm 칸에 그리면서 원본 해상도를
    그대로 품으면, **6컷 콘티가 30MB 짜리 파일이 된다**(실제로 그랬다). 12컷이면 60MB 다.
    회의에 돌리려고 만든 문서인데 메일에 첨부할 수 없는 크기가 되어 목적을 잃는다.

    화면에 40mm 로 나올 그림에 1024px 을 담는 것은 어차피 버려지는 화소다. 인쇄
    품질(300DPI)에 필요한 만큼만 남기고 JPEG 로 다시 쓴다. 컷 그림은 사진에 가까워
    JPEG 가 PNG 보다 훨씬 작다.

    투명도는 흰 배경에 합성한다. JPEG 은 알파를 담지 못하고, 표 배경이 흰색이다.

    Pillow 로 열지 못하면 원본을 그대로 넘긴다 — 크기를 줄이지 못해도 문서는 나와야 한다.
    """
    ratio = 9 / 16
    out = data
    try:
        from PIL import Image as PILImage

        with PILImage.open(io.BytesIO(data)) as probe:
            probe.load()
            width, height = probe.size
            if width and height:
                ratio = height / width

            if probe.mode in ("RGBA", "LA", "P"):
                converted = probe.convert("RGBA")
                canvas = PILImage.new("RGB", converted.size, (255, 255, 255))
                canvas.paste(converted, mask=converted.split()[-1])
                image = canvas
            else:
                image = probe.convert("RGB")

            longest = max(image.size)
            if longest > _MAX_EDGE_PX:
                scale = _MAX_EDGE_PX / longest
                image = image.resize(
                    (max(1, round(image.width * scale)), max(1, round(image.height * scale))),
                    PILImage.LANCZOS,
                )

            buffer = io.BytesIO()
            image.save(buffer, format="JPEG", quality=_JPEG_QUALITY, optimize=True)
            out = buffer.getvalue()
    except Exception as error:  # noqa: BLE001 - 줄이지 못해도 원본으로 넣는다
        logger.warning("콘티 PDF 이미지를 줄이지 못했습니다(원본을 씁니다): %s", error)

    draw_w = _FRAME_W
    draw_h = draw_w * ratio
    if draw_h > _FRAME_H:
        draw_h = _FRAME_H
        draw_w = (draw_h / ratio) if ratio else _FRAME_W
    return out, draw_w, draw_h


def _frame_cell(cut: dict, style: dict, image_loader) -> tuple:
    """Video 칸. `(flowable, 그림을_넣었는가)` 를 돌려준다.

    그림이 없으면 그 이유를 적는다. 읽기 실패와 깨진 파일도 여기서 흡수한다 —
    한 컷 때문에 문서 전체를 잃는 것이 더 나쁘다.
    """
    key = cut.get("imageKey") or ""
    if key and image_loader:
        data = None
        try:
            data = image_loader(key)
        except Exception as error:  # noqa: BLE001
            logger.warning("콘티 PDF 이미지를 읽지 못했습니다 key=%s: %s", key, error)
        if data:
            try:
                payload, width, height = _prepare_image(data)
                return Image(io.BytesIO(payload), width=width, height=height), True
            except Exception as error:  # noqa: BLE001 - 깨진 파일
                logger.warning("콘티 PDF 이미지를 넣지 못했습니다 key=%s: %s", key, error)

    note = {
        "pending": "그리는 중",
        "drawing": "그리는 중",
        "failed": "그림 실패",
    }.get(str(cut.get("status") or ""), "그림 없음")
    if cut.get("error"):
        note = f"{note}<br/>{_text(cut['error'])[:80]}"
    elif key:
        # 키는 있는데 못 실었다. 사용자가 "왜 화면에는 보이는데 PDF 에는 없나"를 묻게 된다.
        note = "그림을 불러오지 못했습니다"
    # 그림이 있는 행과 높이를 비슷하게 둔다. 없는 칸이 납작해지면 표가 들쭉날쭉해진다.
    return [Spacer(1, 7 * mm), Paragraph(note, style["frameNote"])], False


def _audio_cell(cut: dict, style: dict):
    """Audio 칸. 대사 · 음향 · 메모. 참고 문서와 같은 라벨과 순서."""
    lines = [_label(name, value)
             for name, value in (("대사", cut.get("dialogue")),
                                 ("음향", cut.get("audio")),
                                 ("메모", cut.get("note")))
             if str(value or "").strip()]
    if not lines:
        return Paragraph("-", style["dim"])
    return Paragraph("<br/>".join(lines), style["body"])


# --------------------------------------------------------------------------- #
# 대본
# --------------------------------------------------------------------------- #
def script_pdf(title: str, episodes: list[dict], *, meta: str = "") -> bytes:
    """대본 PDF. 세로 A4.

    `episodes` 는 [{no, title, payload}] 다. payload 는 회차 본문
    (scenes[].slug/action/dialogue[], summary, ending_hook). 회차가 여러 개면 회차마다
    페이지를 나눈다 — 대본은 회차 단위로 읽고 돌린다.
    """
    style = _styles()
    buffer = io.BytesIO()
    doc = _doc(
        buffer, title or "대본", "AI 생성 대본",
        leftMargin=22 * mm, rightMargin=18 * mm, topMargin=18 * mm, bottomMargin=16 * mm,
    )

    story: list = [Paragraph(_text(title or "대본"), style["title"])]
    if meta:
        story.append(Paragraph(_text(meta), style["meta"]))

    for position, episode in enumerate(episodes):
        if position:
            story.append(PageBreak())
        story.extend(_episode_flowables(episode, style))

    return _build(doc, story, buffer)


def _episode_flowables(episode: dict, style: dict) -> list:
    payload = episode.get("payload") or {}
    out: list = []

    heading = episode.get("title") or payload.get("title") or ""
    if episode.get("no"):
        heading = f"{episode['no']}화" + (f" · {heading}" if heading else "")
    if heading:
        out += [Spacer(1, 3 * mm), Paragraph(_text(heading), style["slug"])]

    summary = _text(payload.get("summary"))
    if summary:
        out.append(Paragraph(_label("요약", payload.get("summary")), style["note"]))

    for index, scene in enumerate(payload.get("scenes") or [], start=1):
        if not isinstance(scene, dict):
            continue
        slug = _text(scene.get("slug")) or f"S#{index}"
        # 씬 제목과 첫 지문을 붙여 둔다. 제목만 페이지 끝에 남으면 읽는 흐름이 끊긴다.
        block = [Paragraph(slug, style["slug"])]
        action = _text(scene.get("action"))
        if action:
            block.append(Paragraph(action, style["action"]))
        out.append(KeepTogether(block))

        for cue in (scene.get("dialogue") or []):
            if not isinstance(cue, dict):
                continue
            line = _text(cue.get("line"))
            if not line:
                continue
            # 인물명과 대사는 붙어 있어야 누가 말하는지 알 수 있다.
            out.append(KeepTogether([
                Paragraph(_text(cue.get("character")) or "대사", style["character"]),
                Paragraph(line, style["line"]),
            ]))

    hook = _text(payload.get("ending_hook"))
    if hook:
        out.append(Paragraph(
            f"<font name='{SANS}' size=7 color='#5234c1'>엔딩 훅</font><br/>{hook}", style["hook"]
        ))
    return out


# --------------------------------------------------------------------------- #
# 로그라인
# --------------------------------------------------------------------------- #
def logline_pdf(title: str, payload: dict, *, meta: str = "") -> bytes:
    """로그라인 PDF. 안(案)마다 한 덩어리로 묶어 페이지 사이에서 쪼개지지 않게 한다."""
    style = _styles()
    buffer = io.BytesIO()
    doc = _doc(
        buffer, title or "로그라인", "AI 생성 로그라인",
        leftMargin=20 * mm, rightMargin=18 * mm, topMargin=18 * mm, bottomMargin=16 * mm,
    )

    story: list = [Paragraph(_text(title or "로그라인"), style["title"])]
    if meta:
        story.append(Paragraph(_text(meta), style["meta"]))

    recommended = payload.get("recommended")
    for index, option in enumerate(payload.get("options") or [], start=1):
        if not isinstance(option, dict):
            continue
        no = option.get("no") or index
        label = f"{no}안"
        if option.get("angle"):
            label += f" · {_text(option['angle'])}"
        try:
            if recommended is not None and int(recommended) == int(no):
                label += " (추천)"
        except (TypeError, ValueError):
            pass

        block = [Paragraph(label, style["slug"])]
        if option.get("logline"):
            block.append(Paragraph(_text(option["logline"]), style["action"]))
        for name, value in (("장르", option.get("genre")),
                            ("톤", option.get("tone")),
                            ("고른 이유", option.get("why"))):
            if str(value or "").strip():
                block.append(Paragraph(_label(name, value), style["note"]))
        story.append(KeepTogether(block))

    return _build(doc, story, buffer)
