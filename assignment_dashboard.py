import json
import uuid
from datetime import datetime, date, time
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import requests
import streamlit as st

# --- 설정 ---
BASE_URL = "https://cyber.jj.ac.kr"
TOKEN_URL = f"{BASE_URL}/login/token.php"
API_URL = f"{BASE_URL}/webservice/rest/server.php"
KST = ZoneInfo("Asia/Seoul")
MANUAL_FILE = Path(__file__).with_name("my_assignments.json")

# 단순화된 상태 라벨
STATUS_LABEL = {"submitted": "✅ 제출", "new": "❌ 미제출"}

# ───────────────────────── 데이터 저장/불러오기 ─────────────────────────
def load_manual() -> list:
    if MANUAL_FILE.exists():
        try:
            return json.loads(MANUAL_FILE.read_text(encoding="utf-8"))
        except Exception:
            return []
    return []

def save_manual(items: list) -> None:
    MANUAL_FILE.write_text(json.dumps(items, ensure_ascii=False, indent=2),
                           encoding="utf-8")

# ───────────────────────── API ─────────────────────────
def login(username: str, password: str) -> str:
    """Moodle 로그인 및 API 토큰 발급"""
    r = requests.post(
        TOKEN_URL,
        data={"username": username, "password": password,
              "service": "moodle_mobile_app"},
        timeout=20,
    )
    r.raise_for_status()
    result = r.json()
    if "token" not in result:
        raise Exception(result.get("error", "토큰 발급 실패"))
    return result["token"]

def call_api(token: str, wsfunction: str, **params):
    r = requests.post(
        API_URL,
        data={"wstoken": token, "wsfunction": wsfunction,
              "moodlewsrestformat": "json", **params},
        timeout=30,
    )
    r.raise_for_status()
    result = r.json()
    if isinstance(result, dict) and "exception" in result:
        raise Exception(result.get("message", "API 오류"))
    return result

@st.cache_data(ttl=300, show_spinner=False)
def get_assignments(token: str):
    """과제 정보 조회"""
    result = call_api(token, "mod_assign_get_assignments")
    if not isinstance(result, dict):
        raise Exception(f"예상하지 못한 응답: {result}")
    return result.get("courses", [])

@st.cache_data(ttl=300, show_spinner=False)
def get_submission_status(token: str, assign_id: int) -> str:
    """과제 제출 상태 조회 (실시간 확인용)"""
    try:
        res = call_api(token, "mod_assign_get_submission_status",
                       assignid=assign_id)
        # Moodle 응답 구조에 따라 status 추출
        sub = (res.get("lastattempt") or {}).get("submission") or {}
        status_raw = sub.get("status", "new")
        
        if status_raw == "submitted":
            return STATUS_LABEL["submitted"]
        else:
            return STATUS_LABEL["new"]
    except Exception:
        return "❔ 확인불가"

# ───────────────────────── 유틸리티 ─────────────────────────
def format_remaining(td) -> str:
    days, sec = td.days, td.seconds
    return f"{days}일 {sec // 3600}시간 {(sec % 3600) // 60}분"

def urgency(hours_left: float) -> str:
    if hours_left <= 0: return "⚫ 마감"
    if hours_left <= 24: return "🔴 24시간 이내"
    if hours_left <= 72: return "🟠 3일 이내"
    if hours_left <= 168: return "🟡 7일 이내"
    return "🟢 여유"

# ───────────────────────── 데이터 가공 ─────────────────────────
def build_dataframe(courses, token, manual: list) -> pd.DataFrame:
    now = datetime.now(KST)
    rows = []

    # 1. 직접 추가한 과제
    for m in manual:
        try:
            due = datetime.fromisoformat(m["due"]).replace(tzinfo=KST)
            remaining = due - now
            hours_left = remaining.total_seconds() / 3600
            
            if hours_left < 0:
                continue

            rows.append({
                "강의": m.get("course") or "기타",
                "과제명": m["name"],
                "마감일": due,
                "남은시간(h)": hours_left,
                "남은 시간": format_remaining(remaining),
                "긴급도": urgency(hours_left),
                "제출상태": STATUS_LABEL["submitted"] if m.get("done") else STATUS_LABEL["new"],
                "메모": m.get("memo", ""),
                "주소": m.get("url") or None,
            })
        except:
            continue

    # 2. 사이버캠퍼스 과제 (실시간 상태 확인 포함)
    for course in courses:
        course_name = course.get("fullname", "강의명 없음")
        for a in course.get("assignments", []):
            due_ts = a.get("duedate", 0)
            if not due_ts: continue
            
            due = datetime.fromtimestamp(due_ts, KST)
            remaining = due - now
            hours_left = remaining.total_seconds() / 3600

            if hours_left < 0:
                continue

            # [핵심] 실시간 제출 상태 확인 호출
            status = get_submission_status(token, a.get("id"))

            rows.append({
                "강의": course_name,
                "과제명": a.get("name", "과제명 없음"),
                "마감일": due,
                "남은시간(h)": hours_left,
                "남은 시간": format_remaining(remaining),
                "긴급도": urgency(hours_left),
                "제출상태": status,
                "메모": "",
                "주소": f"{BASE_URL}/mod/assign/view.php?id={a['cmid']}" if a.get("cmid") else None,
            })
            
    df = pd.DataFrame(rows)
    if not df.empty:
        df = df.sort_values("마감일").reset_index(drop=True)
    return df

# ───────────────────────── UI ─────────────────────────
st.set_page_config(page_title="JJ 과제", page_icon="📚", layout="wide")
st.title("📚 과제 확인")

if "token" not in st.session_state:
    st.session_state.token = None

# 사이드바
with st.sidebar:
    st.header("🔐 로그인")
    if st.session_state.token is None:
        with st.form("login_form"):
            username = st.text_input("학번")
            password = st.text_input("비밀번호", type="password")
            if st.form_submit_button("로그인", use_container_width=True):
                try:
                    with st.spinner("로그인 중..."):
                        st.session_state.token = login(username, password)
                    st.rerun()
                except Exception as e:
                    st.error(f"로그인 실패: {e}")
    else:
        st.success("로그인됨")
        if st.button("🔄 새로고침", use_container_width=True):
            st.cache_data.clear()
            st.rerun()
        if st.button("로그아웃", use_container_width=True):
            st.session_state.token = None
            st.cache_data.clear()
            st.rerun()

if st.session_state.token is None:
    st.info("사이버캠퍼스 데이터를 확인하려면 로그인을 해주세요.")
    st.stop()

token = st.session_state.token
manual = load_manual()

# 데이터 로드
try:
    with st.spinner("과제 목록 및 제출 상태를 확인하고 있습니다... (잠시만 기다려주세요)"):
        courses = get_assignments(token)
        course_names = sorted({c.get("fullname", "") for c in courses if c.get("fullname")})
except Exception as e:
    st.error(f"데이터를 가져오는 중 오류가 발생했습니다: {e}")
    st.stop()

# ───────────── 과제 직접 추가 ─────────────
with st.expander("➕ 과제 직접 추가하기"):
    with st.form("add_assignment", clear_on_submit=True):
        a1, a2 = st.columns(2)
        course_choice = a1.selectbox("강의", ["(직접 입력)"] + course_names)
        course_custom = a2.text_input("강의명 직접 입력", placeholder="예: 팀 프로젝트")
        name = st.text_input("과제명 *")
        d1, d2 = st.columns(2)
        due_date = d1.date_input("마감 날짜", value=date.today())
        due_time = d2.time_input("마감 시간", value=time(23, 59))
        url = st.text_input("관련 링크 (선택)")
        memo = st.text_area("메모 (선택)", height=80)
        if st.form_submit_button("추가", type="primary"):
            if name.strip():
                course_val = course_custom.strip() if course_choice == "(직접 입력)" else course_choice
                manual.append({
                    "id": uuid.uuid4().hex,
                    "course": course_val or "기타",
                    "name": name.strip(),
                    "due": datetime.combine(due_date, due_time).isoformat(),
                    "url": url.strip(),
                    "memo": memo.strip(),
                    "done": False,
                })
                save_manual(manual)
                st.success("추가되었습니다.")
                st.rerun()
            else:
                st.warning("과제명을 입력해주세요.")

# ───────────── 직접 추가한 과제 관리 ─────────────
if manual:
    with st.expander(f"✍️ 직접 추가한 과제 관리 ({len(manual)}개)"):
        for m in sorted(manual, key=lambda x: x["due"]):
            c1, c2, c3 = st.columns([1, 6, 1])
            done = c1.checkbox("완료", value=m.get("done", False), key=f"done_{m['id']}")
            if done != m.get("done", False):
                m["done"] = done
                save_manual(manual)
                st.rerun()
            
            due_str = datetime.fromisoformat(m["due"]).strftime("%Y-%m-%d %H:%M")
            title = f"~~{m['name']}~~" if done else f"**{m['name']}**"
            c2.markdown(f"{title} · {m['course']} · 마감 {due_str}")
            if c3.button("🗑️ 삭제", key=f"del_{m['id']}"):
                manual = [x for x in manual if x["id"] != m["id"]]
                save_manual(manual)
                st.rerun()

# 데이터프레임 생성
try:
    df = build_dataframe(courses, token, manual)
except Exception as e:
    st.error(f"데이터 구성 오류: {e}")
    st.stop()

if df.empty:
    st.success("현재 진행 중인 과제가 없습니다. 🎉")
    st.stop()

# ───────────── 메인 출력 ─────────────
# KPI 영역
c1, c2, c3, c4, c5 = st.columns(5)
c1.metric("수강 강의", len(courses))
c2.metric("진행 과제", len(df))
c3.metric("미제출", len(df[df["제출상태"] == STATUS_LABEL["new"]]))
c4.metric("🔴 24h 이내", int((df["남은시간(h)"] <= 24).sum()))
c5.metric("🟡 7일 이내", int((df["남은시간(h)"] <= 168).sum()))

# 필터 및 목록
st.subheader("📋 과제 목록")
f1, f2, f3 = st.columns([2, 2, 2])
sel_courses = f1.multiselect("강의 필터", sorted(df["강의"].unique()))
sel_urgency = f2.multiselect("긴급도 필터", sorted(df["긴급도"].unique()))
only_pending = f3.checkbox("미제출만 보기")

view = df.copy()
if sel_courses: view = view[view["강의"].isin(sel_courses)]
if sel_urgency: view = view[view["긴급도"].isin(sel_urgency)]
if only_pending: view = view[view["제출상태"] == STATUS_LABEL["new"]]

cols = ["긴급도", "제출상태", "과제명", "강의", "마감일", "남은 시간", "메모", "주소"]

st.dataframe(
    view[cols],
    use_container_width=True,
    hide_index=True,
    column_config={
        "마감일": st.column_config.DatetimeColumn(format="YYYY-MM-DD HH:mm"),
        "주소": st.column_config.LinkColumn("바로가기", display_text="열기"),
    },
)

st.caption(f"마지막 조회: {datetime.now(KST):%Y-%m-%d %H:%M:%S}")
