import streamlit as st
import requests
from datetime import datetime, date as ddate, timedelta, time as dtime
from zoneinfo import ZoneInfo

# =========================
# PAGE CONFIG & TITLE
# =========================
st.set_page_config(page_title="NHL Live", page_icon="🏒", layout="wide")
st.title("🏒 NHL Dashboard")

st.components.v1.html("""
<script>
(function() {
    const orig = Intl.DateTimeFormat;
    Intl.DateTimeFormat = function(l, o) { return new orig('en-CA', o); };
    Intl.DateTimeFormat.supportedLocalesOf = orig.supportedLocalesOf.bind(orig);
})();
</script>
""", height=0)

# =========================
# CONSTANTS
# =========================
ET = ZoneInfo("America/New_York")
ESPN_SCOREBOARD = "https://site.api.espn.com/apis/site/v2/sports/hockey/nhl/scoreboard"
ESPN_SUMMARY    = "https://site.api.espn.com/apis/site/v2/sports/hockey/nhl/summary"
NHL_SCHEDULE    = "https://api-web.nhle.com/v1/schedule"
NHL_PBP         = "https://api-web.nhle.com/v1/gamecenter"

PLAY_EMOJI = {
    "goal": "🚨", "penalty": "🟡", "shot-on-goal": "🎯",
    "blocked-shot": "🛡️", "missed-shot": "🤦", "faceoff": "🏒",
    "hit": "💥", "giveaway": "❌", "takeaway": "⛹️",
    "stoppage": "⏸️", "period-start": "▶️", "period-end": "⏹️",
}

# Fuzzy match window in seconds
FUZZY_SECONDS = 2

# =========================
# SESSION STATE INIT
# =========================
for k, v in {
    "view": "schedule",
    "event_id": None, "nhl_game_id": None,
    "away": "", "home": "",
    "away_logo": "", "home_logo": "",
    "away_score": None, "home_score": None,
    "game_state": "",
    "filters_applied": False,
    "filtered_plays": None,
    "cached_plays": None,
    "cached_event_id": None,
    "last_refresh": datetime.now(ET),
    "sched_date": ddate.today(),
}.items():
    if k not in st.session_state:
        st.session_state[k] = v

# =========================
# HELPERS
# =========================
def reset_filter_state():
    """Clears logic and UI ticks."""
    st.session_state.filters_applied = False
    st.session_state.filtered_plays = None
    
    # Reset specific widget keys to uncheck boxes
    keys_to_reset = ["f_period", "f_period_sel", "f_time", "f_goal", "f_pp", "f_en"]
    for k in keys_to_reset:
        if k in st.session_state:
            st.session_state[k] = [] if "sel" in k else False

def to_et(raw: str):
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).astimezone(ET)
    except Exception:
        return None

def fmt_et_full(raw: str) -> str:
    dt = to_et(raw)
    if not dt:
        return "N/A"
    label = "EDT" if dt.dst() != timedelta(0) else "EST"
    return dt.strftime(f"%Y-%m-%d %H:%M:%S {label}")

def fmt_game_time(dt) -> str:
    return dt.strftime("%H:%M ET") if dt else "TBD"

def get_play_emoji(type_str: str) -> str:
    t = (type_str or "").lower()
    for k, v in PLAY_EMOJI.items():
        if k in t:
            return v
    return "🏒"

def period_label(period_num, period_type: str = "") -> str:
    if "overtime" in (period_type or "").lower() or (isinstance(period_num, int) and period_num > 3):
        ot_num = period_num - 3
        return f"OT{ot_num}" if ot_num > 1 else "OT"
    if "shootout" in (period_type or "").lower():
        return "SO"
    return f"P{period_num}"

def espn_clock_to_seconds(clock_str: str, period_num: int) -> int:
    try:
        parts = clock_str.strip().split(":")
        mins, secs = int(parts[0]), int(parts[1])
        elapsed = mins * 60 + secs
    except Exception:
        elapsed = 0
    period_offset = (period_num - 1) * 1200
    return period_offset + elapsed

def nhl_clock_to_seconds(time_in_period: str, period_num: int) -> int:
    try:
        parts = time_in_period.strip().split(":")
        mins, secs = int(parts[0]), int(parts[1])
        remaining  = mins * 60 + secs
        elapsed    = 1200 - remaining
    except Exception:
        elapsed = 0
    period_offset = (period_num - 1) * 1200
    return period_offset + elapsed

def parse_nhl_situation(sit_code: str) -> str:
    if not sit_code or len(sit_code) < 4:
        return ""
    away_g = sit_code[0]
    away_sk = sit_code[1]
    home_sk = sit_code[2]
    home_g = sit_code[3]
    try:
        a = int(away_sk)
        h = int(home_sk)
    except ValueError:
        return ""
    is_away_en = (away_g == "0")
    is_home_en = (home_g == "0")
    is_pp = (a != h)
    parts = [f"{a}v{h}"]
    if is_away_en and is_home_en: parts.append("Both EN")
    elif is_away_en: parts.append("Away EN")
    elif is_home_en: parts.append("Home EN")
    if is_pp: parts.append("PP")
    return " ".join(parts)

# =========================
# NHL GAME ID LOOKUP
# =========================
@st.cache_data(ttl=3600, show_spinner=False)
def find_nhl_game_id(date_str: str, away_abbr: str, home_abbr: str) -> str:
    try:
        resp = requests.get(f"{NHL_SCHEDULE}/{date_str}", timeout=10)
        resp.raise_for_status()
        data = resp.json()
    except Exception:
        return ""
    target_date = datetime.fromisoformat(date_str).date()
    for day in data.get("gameWeek", []):
        for g in day.get("games", []):
            start_utc = g.get("startTimeUTC", "")
            start_dt  = to_et(start_utc)
            if start_dt and start_dt.date() != target_date:
                continue
            nhl_away = g.get("awayTeam", {}).get("abbrev", "").upper()
            nhl_home = g.get("homeTeam", {}).get("abbrev", "").upper()
            if _abbr_match(away_abbr, nhl_away) and _abbr_match(home_abbr, nhl_home):
                return str(g.get("id", ""))
    return ""

def _abbr_match(espn_abbr: str, nhl_abbr: str) -> bool:
    espn_abbr = espn_abbr.upper()
    nhl_abbr  = nhl_abbr.upper()
    if espn_abbr == nhl_abbr:
        return True
    KNOWN = {
        "VGK": ["VEG", "VGK", "LV"], "VEG": ["VGK", "VEG", "LV"], "LV": ["VGK", "VEG"],
        "UTA": ["UTAH", "UTA"], "WSH": ["WAS", "WSH"], "WAS": ["WSH", "WAS"],
        "CLB": ["CBJ", "CLB"], "CBJ": ["CLB", "CBJ"],
    }
    aliases = KNOWN.get(espn_abbr, []) + KNOWN.get(nhl_abbr, [])
    return espn_abbr in aliases or nhl_abbr in aliases

# =========================
# NHL PLAY-BY-PLAY FETCH
# =========================
@st.cache_data(ttl=30, show_spinner=False)
def fetch_nhl_plays(nhl_game_id: str) -> list:
    if not nhl_game_id:
        return []
    try:
        resp = requests.get(f"{NHL_PBP}/{nhl_game_id}/play-by-play", timeout=10)
        resp.raise_for_status()
        plays = resp.json().get("plays", [])
    except Exception:
        return []
    result = []
    for p in plays:
        pd  = p.get("periodDescriptor", {})
        pnum = pd.get("number", 1)
        ptype = pd.get("periodType", "REG")
        tip = p.get("timeInPeriod", "20:00")
        sit = p.get("situationCode") or ""
        type_key= p.get("typeDescKey", "")
        elapsed = nhl_clock_to_seconds(tip, pnum)
        result.append({
            "period": pnum, "period_type": ptype, "elapsed": elapsed,
            "time_in": tip, "type_key": type_key, "sit_code": sit,
            "situation": parse_nhl_situation(sit), "sort_order": p.get("sortOrder", 0),
        })
    return result

def build_situation_windows(nhl_plays: list) -> list:
    if not nhl_plays:
        return []
    BOUNDARY_TYPES = {"period-start", "period-end", "game-start", "game-end", "shootout-start", "shootout-end"}
    sorted_plays = sorted(nhl_plays, key=lambda p: (p["period"], p["elapsed"], p["sort_order"]))
    raw_windows  = []
    prev_sit, win_start, win_sit_code, win_sit, win_boundary = None, 0, "", "", False

    for p in sorted_plays:
        sit = p["sit_code"]
        if sit == prev_sit: continue
        if prev_sit is not None:
            raw_windows.append([win_start, p["elapsed"], win_sit_code, win_sit, win_boundary])
        win_start, win_sit_code, win_sit = p["elapsed"], sit, p["situation"]
        win_boundary = p.get("type_key", "") in BOUNDARY_TYPES
        prev_sit = sit
    if prev_sit is not None:
        raw_windows.append([win_start, 99999, win_sit_code, win_sit, win_boundary])

    # Validation and Merging
    penalty_times = sorted([p["elapsed"] for p in nhl_plays if p.get("type_key", "") in ("penalty", "delayed-penalty")])
    validated = []
    for w in raw_windows:
        w_start, w_end, w_code, w_sit_str, w_bnd = w
        is_pp = "PP" in w_sit_str and "EN" not in w_sit_str
        if is_pp:
            if (w_end - w_start < 30) or not any(w_start-300 <= t <= w_start+60 for t in penalty_times):
                validated.append([w_start, w_end, "5v5"])
                continue
        validated.append([w_start, w_end, w_sit_str])

    final = []
    for w in validated:
        if final and final[-1][2] == w[2]: final[-1][1] = w[1]
        else: final.append(w)
    return [(w[0], w[1], w[2]) for w in final if w[1] > w[0]]

def find_nhl_situation(espn_play: dict, windows: list) -> str:
    if not windows: return ""
    elapsed = espn_play.get("elapsed", 0)
    for (w_start, w_end, w_sit) in windows:
        if w_start <= elapsed < w_end: return w_sit
    if windows and elapsed >= windows[-1][0]: return windows[-1][2]
    return ""

# =========================
# ESPN FETCHING
# =========================
@st.cache_data(ttl=30, show_spinner=False)
def fetch_scoreboard(date_str: str) -> list:
    date_compact = date_str.replace("-", "")
    try:
        resp = requests.get(ESPN_SCOREBOARD, params={"dates": date_compact, "limit": 25}, timeout=10)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        st.error(f"ESPN error: {e}")
        return []
    games = []
    for event in data.get("events", []):
        comp = event.get("competitions", [{}])[0]
        status = comp.get("status", {})
        state_type = status.get("type", {})
        state = state_type.get("state", "pre")
        display_status = "Scheduled" if state_type.get("name") == "STATUS_SCHEDULED" else state_type.get("shortDetail", "")
        competitors = comp.get("competitors", [])
        try:
            away = next(c for c in competitors if c.get("homeAway") == "away")
            home = next(c for c in competitors if c.get("homeAway") == "home")
        except StopIteration: continue
        games.append({
            "event_id": event.get("id", ""), "state": state, "state_name": display_status,
            "is_live": state == "in", "is_final": state == "post", "is_ot": status.get("period", 0) > 3,
            "away_abbr": away.get("team", {}).get("abbreviation", "?"), "home_abbr": home.get("team", {}).get("abbreviation", "?"),
            "away_logo": away.get("team", {}).get("logo", ""), "home_logo": home.get("team", {}).get("logo", ""),
            "away_score": int(away.get("score", 0) or 0), "home_score": int(home.get("score", 0) or 0),
            "has_score": state in ["in", "post"], "time_str": fmt_game_time(to_et(event.get("date", ""))), "date_str": date_str,
        })
    return sorted(games, key=lambda x: x["time_str"])

def get_parsed_plays(event_id, nhl_game_id):
    st.session_state.last_refresh = datetime.now(ET)
    if st.session_state.cached_event_id == event_id and st.session_state.cached_plays:
        return st.session_state.cached_plays
    try:
        resp = requests.get(ESPN_SUMMARY, params={"event": event_id}, timeout=15)
        resp.raise_for_status()
        raw_plays = resp.json().get("plays", [])
    except Exception as e:
        st.error(f"ESPN error: {e}"); return []
    
    windows = build_situation_windows(fetch_nhl_plays(nhl_game_id))
    plays = []
    for p in raw_plays:
        pnum = p.get("period", {}).get("number", 1) if isinstance(p.get("period"), dict) else 1
        clock_val = p.get("clock", {}).get("displayValue", "") if isinstance(p.get("clock"), dict) else str(p.get("clock"))
        elapsed = espn_clock_to_seconds(clock_val, pnum)
        plays.append({
            "seq": int(p.get("sequenceNumber", 0)), "period_label": period_label(pnum, p.get("period", {}).get("type", "")),
            "clock": clock_val, "elapsed": elapsed, "type_text": p.get("type", {}).get("text", ""),
            "text": p.get("text", ""), "situation": find_nhl_situation({"elapsed": elapsed}, windows),
            "wall_et": fmt_et_full(p.get("wallclock", "")), "wall_dt": to_et(p.get("wallclock", "")),
            "away_score": p.get("awayScore", ""), "home_score": p.get("homeScore", ""),
            "emoji": get_play_emoji(p.get("type", {}).get("text", "")),
        })
    plays.sort(key=lambda x: x["seq"])
    st.session_state.cached_plays, st.session_state.cached_event_id = plays, event_id
    return plays

# =========================
# SHARED CSS
# =========================
st.markdown("""
<style>
div[data-testid="stVerticalBlockBorderWrapper"] { min-height: 150px; }
.sched-team-row { display: flex; align-items: center; gap: 12px; margin-bottom: 6px; }
.sched-team-name { font-size: 22px; font-weight: 800; color: #ffffff; }
.sched-score { font-size: 22px; font-weight: 800; color: #888888; margin-left: auto; }
.sched-meta { font-size: 13px; color: #999999; border-top: 1px solid rgba(255,255,255,0.1); padding-top: 8px; margin-top: 8px; display: flex; align-items: center; }
.sched-extra { background: #e67e22; color: #fff; font-size: 11px; padding: 2px 6px; border-radius: 4px; margin-left: 8px; font-weight: bold; }
</style>
""", unsafe_allow_html=True)

# ======================================================
# GAME FEED VIEW
# ======================================================
if st.session_state.view == "game":
    plays = get_parsed_plays(st.session_state.event_id, st.session_state.nhl_game_id)

    nav_col1, nav_col2, nav_col3, _ = st.columns([1.3, 1, 1.8, 5.9])
    with nav_col1:
        if st.button("⬅ Back to Schedule", use_container_width=True):
            st.session_state.view = "schedule"
            reset_filter_state()
            st.rerun()
    with nav_col2:
        if st.button("🔄 Refresh", use_container_width=True):
            st.session_state.cached_plays = None
            st.rerun()
    with nav_col3:
        st.markdown(f'<div style="background-color:#2e7d32;color:white;padding:8px 16px;border-radius:4px;font-size:14px;font-weight:bold;text-align:center;">Last refresh {st.session_state.last_refresh.strftime("%H:%M:%S ET")}</div>', unsafe_allow_html=True)

    st.markdown("<br>", unsafe_allow_html=True)
    head_c1, head_c2, head_c3 = st.columns([1, 6, 1])
    with head_c1:
        if st.session_state.away_logo: st.image(st.session_state.away_logo, width=80)
    with head_c2:
        st.markdown(f'<div style="display:flex;align-items:center;justify-content:center;font-weight:800;font-size:32px;gap:15px;"><span>{st.session_state.away}</span><span style="color:#888;">{st.session_state.away_score}</span><span>-</span><span style="color:#888;">{st.session_state.home_score}</span><span>{st.session_state.home}</span></div>', unsafe_allow_html=True)
    with head_c3:
        if st.session_state.home_logo: st.image(st.session_state.home_logo, width=80)

    st.divider()

    # ── FILTERS ──────────────────────────────────────────────────────────
    all_periods = sorted(list({p["period_label"] for p in plays}))
    
    USE_PERIOD_FILTER = st.checkbox("🏒 Filter by Period", key="f_period", value=False, on_change=reset_filter_state)
    selected_periods = st.multiselect("Select Periods", options=all_periods, key="f_period_sel") if USE_PERIOD_FILTER else []

    USE_TIME_FILTER = st.checkbox("🕐 Filter by Actual Time (ET)", key="f_time", value=False, on_change=reset_filter_state)
    START_DT = END_DT = None
    if USE_TIME_FILTER:
        sc1, sc2 = st.columns(2)
        with sc1: start_date_input = st.date_input("Start date", value=ddate.today())
        with sc2: start_time_input = st.time_input("Start time", value=dtime(19, 0))
        ec1, ec2 = st.columns(2)
        with ec1: end_date_input = st.date_input("End date", value=ddate.today())
        with ec2: end_time_input = st.time_input("End time", value=dtime(23, 59))
        START_DT = datetime.combine(start_date_input, start_time_input).replace(tzinfo=ET)
        END_DT = datetime.combine(end_date_input, end_time_input).replace(tzinfo=ET)

    USE_GOAL_FILTER = st.checkbox("🚨 Goals Only", key="f_goal", value=False, on_change=reset_filter_state)
    USE_PP_FILTER   = st.checkbox("⚡ Power Plays Only", key="f_pp", value=False, on_change=reset_filter_state)
    USE_GP_FILTER   = st.checkbox("🥅 Empty Nets Only", key="f_en", value=False, on_change=reset_filter_state)

    # Side-by-side buttons
    btn_col1, btn_col2, _ = st.columns([1.5, 1.5, 7])
    with btn_col1:
        if st.button("🚀 Apply Filters", use_container_width=True):
            def passes(p):
                sit = p.get("situation", "")
                if USE_PERIOD_FILTER and selected_periods and p["period_label"] not in selected_periods: return False
                if USE_TIME_FILTER:
                    if not p["wall_dt"] or not (START_DT <= p["wall_dt"] <= END_DT): return False
                if USE_GOAL_FILTER and p["type_text"] != "Goal": return False
                if USE_PP_FILTER and "PP" not in sit: return False
                if USE_GP_FILTER and "EN" not in sit: return False
                return True
            st.session_state.filtered_plays = [p for p in plays if passes(p)]
            st.session_state.filters_applied = True
            st.rerun()

    with btn_col2:
        if st.button("🗑️ Remove Filters", use_container_width=True):
            reset_filter_state()
            st.rerun()

    # ── DISPLAY LOGIC ────────────────────────────────────────────────────
    filters_applied = st.session_state.get("filters_applied", False)
    display_list = st.session_state.filtered_plays if filters_applied else plays
    total = len(plays)
    showing = len(display_list)

    if filters_applied:
        if showing == 0:
            st.warning("⚠️ No results found.")
            st.stop()
        if USE_PERIOD_FILTER:
            st.info(f"🏒 **Period filter active** — showing **{showing}** of **{total}** plays")
        if USE_GOAL_FILTER:
            st.info(f"🚨 **Goals Only active** — showing **{showing}** of **{total}** plays")
        if USE_PP_FILTER:
            st.info(f"⚡ **Power Plays Only active** — showing **{showing}** of **{total}** plays")
        if USE_GP_FILTER:
            st.info(f"🥅 **Empty Nets Only active** — showing **{showing}** of **{total}** plays")

    for p in display_list:
        emoji = "🚨" if p.get("type_text") == "Goal" else p.get("emoji", "🏒")
        st.subheader(f"{emoji} {p.get('period_label')} | ⏱️ {p.get('clock')}")
        st.markdown(f"📊 **Score:** {p.get('away_score')} - {p.get('home_score')}")
        sit = p.get("situation", "")
        if sit: st.markdown(f"⚖️ **Strength:** `{sit}`")
        st.markdown(f"📋 **Play:** {p.get('text')}")
        if p.get("wall_et") and p.get("wall_et") != "N/A":
            st.markdown(f"🕐 **Time (ET):** `{p['wall_et']}`")
        st.divider()

# ======================================================
# SCHEDULE VIEW
# ======================================================
else:
    date = st.date_input("Select date", value=st.session_state.sched_date)
    st.session_state.sched_date = date
    games = fetch_scoreboard(date.strftime("%Y-%m-%d"))

    if not games:
        st.info(f"No games scheduled for {date}.")
    else:
        cols = st.columns(2)
        for i, g in enumerate(games):
            has_started = g["has_score"]
            ot_badge = '<span class="sched-extra">OT</span>' if g["is_ot"] else ""
            card_html = f"""
            <div class="sched-team-row"><img src="{g['away_logo']}" width="34"/><span class="sched-team-name">{g['away_abbr']}</span><span class="sched-score">{g['away_score'] if has_started else ''}</span></div>
            <div class="sched-team-row"><img src="{g['home_logo']}" width="34"/><span class="sched-team-name">{g['home_abbr']}</span><span class="sched-score">{g['home_score'] if has_started else ''}</span></div>
            <div class="sched-meta">{g['time_str']} &middot; {g['state_name']}{ot_badge}</div>
            """
            with cols[i % 2]:
                with st.container(border=True):
                    st.markdown(card_html, unsafe_allow_html=True)
                    if st.button(f"▶ Open {g['away_abbr']} @ {g['home_abbr']}", key=g['event_id']):
                        st.session_state.event_id = g['event_id']
                        st.session_state.away, st.session_state.home = g['away_abbr'], g['home_abbr']
                        st.session_state.away_logo, st.session_state.home_logo = g['away_logo'], g['home_logo']
                        st.session_state.away_score, st.session_state.home_score = g['away_score'], g['home_score']
                        st.session_state.nhl_game_id = find_nhl_game_id(g['date_str'], g['away_abbr'], g['home_abbr'])
                        st.session_state.view = "game"
                        st.session_state.cached_plays = None
                        st.rerun()
