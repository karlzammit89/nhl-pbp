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
    
    # Keys used in widgets to force reset ticks
    keys_to_reset = ["f_period", "f_period_sel", "f_time", "f_goal", "f_pp", "f_en"]
    for k in keys_to_reset:
        if k in st.session_state:
            st.session_state[k] = [] if "sel" in k else False

def to_et(raw: str):
    if not raw: return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).astimezone(ET)
    except Exception:
        return None

def fmt_et_full(raw: str) -> str:
    dt = to_et(raw)
    if not dt: return "N/A"
    label = "EDT" if dt.dst() != timedelta(0) else "EST"
    return dt.strftime(f"%Y-%m-%d %H:%M:%S {label}")

def fmt_game_time(dt) -> str:
    return dt.strftime("%H:%M ET") if dt else "TBD"

def get_play_emoji(type_str: str) -> str:
    t = (type_str or "").lower()
    for k, v in PLAY_EMOJI.items():
        if k in t: return v
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
    return ((period_num - 1) * 1200) + elapsed

def nhl_clock_to_seconds(time_in_period: str, period_num: int) -> int:
    try:
        parts = time_in_period.strip().split(":")
        mins, secs = int(parts[0]), int(parts[1])
        remaining  = mins * 60 + secs
        elapsed    = 1200 - remaining
    except Exception:
        elapsed = 0
    return ((period_num - 1) * 1200) + elapsed

def parse_nhl_situation(sit_code: str) -> str:
    if not sit_code or len(sit_code) < 4: return ""
    away_g, away_sk, home_sk, home_g = sit_code[0], sit_code[1], sit_code[2], sit_code[3]
    try:
        a, h = int(away_sk), int(home_sk)
    except ValueError:
        return ""
    is_away_en, is_home_en = (away_g == "0"), (home_g == "0")
    parts = [f"{a}v{h}"]
    if is_away_en and is_home_en: parts.append("Both EN")
    elif is_away_en: parts.append("Away EN")
    elif is_home_en: parts.append("Home EN")
    if a != h: parts.append("PP")
    return " ".join(parts)

@st.cache_data(ttl=3600, show_spinner=False)
def find_nhl_game_id(date_str: str, away_abbr: str, home_abbr: str) -> str:
    try:
        resp = requests.get(f"{NHL_SCHEDULE}/{date_str}", timeout=10)
        resp.raise_for_status()
        data = resp.json()
    except Exception: return ""
    target_date = datetime.fromisoformat(date_str).date()
    for day in data.get("gameWeek", []):
        for g in day.get("games", []):
            start_dt = to_et(g.get("startTimeUTC", ""))
            if start_dt and start_dt.date() != target_date: continue
            nhl_away, nhl_home = g.get("awayTeam", {}).get("abbrev", "").upper(), g.get("homeTeam", {}).get("abbrev", "").upper()
            if _abbr_match(away_abbr, nhl_away) and _abbr_match(home_abbr, nhl_home):
                return str(g.get("id", ""))
    return ""

def _abbr_match(e: str, n: str) -> bool:
    e, n = e.upper(), n.upper()
    if e == n: return True
    K = {"VGK":["VEG","VGK","LV"], "VEG":["VGK","VEG","LV"], "LV":["VGK","VEG"], "UTA":["UTAH","UTA"], "WSH":["WAS","WSH"], "WAS":["WSH","WAS"], "CLB":["CBJ","CLB"], "CBJ":["CLB","CBJ"]}
    return e in (K.get(n, []) + [n]) or n in (K.get(e, []) + [e])

@st.cache_data(ttl=30, show_spinner=False)
def fetch_nhl_plays(nhl_game_id: str) -> list:
    if not nhl_game_id: return []
    try:
        resp = requests.get(f"{NHL_PBP}/{nhl_game_id}/play-by-play", timeout=10)
        resp.raise_for_status()
        plays = resp.json().get("plays", [])
    except Exception: return []
    res = []
    for p in plays:
        pd = p.get("periodDescriptor", {})
        pnum, tip = pd.get("number", 1), p.get("timeInPeriod", "20:00")
        res.append({
            "period": pnum, "elapsed": nhl_clock_to_seconds(tip, pnum),
            "type_key": p.get("typeDescKey", ""), "sit_code": p.get("situationCode") or "",
            "situation": parse_nhl_situation(p.get("situationCode") or ""), "sort_order": p.get("sortOrder", 0),
        })
    return res

def build_situation_windows(nhl_plays: list) -> list:
    if not nhl_plays: return []
    sorted_plays = sorted(nhl_plays, key=lambda p: (p["period"], p["elapsed"], p["sort_order"]))
    raw_windows, prev_sit, win_start = [], None, 0
    for p in sorted_plays:
        sit = p["sit_code"]
        if sit == prev_sit: continue
        if prev_sit is not None: raw_windows.append([win_start, p["elapsed"], prev_sit, parse_nhl_situation(prev_sit)])
        win_start, prev_sit = p["elapsed"], sit
    if prev_sit is not None: raw_windows.append([win_start, 99999, prev_sit, parse_nhl_situation(prev_sit)])
    
    # Simple validation: PP needs a penalty logged nearby
    penalty_times = [p["elapsed"] for p in nhl_plays if "penalty" in p["type_key"]]
    validated = []
    for w in raw_windows:
        if "PP" in w[3] and "EN" not in w[3]:
            if not any(w[0]-300 <= t <= w[0]+60 for t in penalty_times):
                w[3] = "5v5"
        validated.append(w)
    return validated

def find_nhl_situation(elapsed, windows) -> str:
    for (w_start, w_end, code, sit) in windows:
        if w_start <= elapsed < w_end: return sit
    return windows[-1][3] if windows else ""

@st.cache_data(ttl=30, show_spinner=False)
def fetch_scoreboard(date_str: str) -> list:
    try:
        resp = requests.get(ESPN_SCOREBOARD, params={"dates": date_str.replace("-",""), "limit": 25}, timeout=10)
        data = resp.json()
    except Exception: return []
    games = []
    for event in data.get("events", []):
        comp = event.get("competitions", [{}])[0]
        status = comp.get("status", {})
        state = status.get("type", {}).get("state", "pre")
        competitors = comp.get("competitors", [])
        away = next(c for c in competitors if c.get("homeAway") == "away")
        home = next(c for c in competitors if c.get("homeAway") == "home")
        games.append({
            "event_id": event.get("id", ""), "state": state, "state_name": status.get("type", {}).get("shortDetail", ""),
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
        raw_plays = resp.json().get("plays", [])
    except Exception: return []
    windows = build_situation_windows(fetch_nhl_plays(nhl_game_id))
    plays = []
    for p in raw_plays:
        pnum = p.get("period", {}).get("number", 1)
        clock = p.get("clock", {}).get("displayValue", "0:00")
        elapsed = espn_clock_to_seconds(clock, pnum)
        plays.append({
            "seq": int(p.get("sequenceNumber", 0)), "period_label": period_label(pnum, p.get("period", {}).get("type", "")),
            "clock": clock, "type_text": p.get("type", {}).get("text", ""), "text": p.get("text", ""),
            "situation": find_nhl_situation(elapsed, windows), "wall_et": fmt_et_full(p.get("wallclock", "")),
            "wall_dt": to_et(p.get("wallclock", "")), "away_score": p.get("awayScore", ""), "home_score": p.get("homeScore", ""),
            "emoji": get_play_emoji(p.get("type", {}).get("text", "")),
        })
    plays.sort(key=lambda x: x["seq"])
    st.session_state.cached_plays, st.session_state.cached_event_id = plays, event_id
    return plays

# =========================
# STYLES
# =========================
st.markdown("""<style>
.sched-team-row { display: flex; align-items: center; gap: 12px; margin-bottom: 6px; }
.sched-team-name { font-size: 22px; font-weight: 800; }
.sched-score { font-size: 22px; font-weight: 800; margin-left: auto; color: #888; }
.sched-meta { font-size: 13px; color: #999; border-top: 1px solid rgba(255,255,255,0.1); padding-top: 8px; }
</style>""", unsafe_allow_html=True)

# =========================
# VIEWS
# =========================
if st.session_state.view == "game":
    plays = get_parsed_plays(st.session_state.event_id, st.session_state.nhl_game_id)
    
    # Nav
    n1, n2, n3, _ = st.columns([1.5, 1, 2, 5])
    with n1:
        if st.button("⬅ Back", use_container_width=True):
            st.session_state.view = "schedule"
            reset_filter_state()
            st.rerun()
    with n2:
        if st.button("🔄 Refresh", use_container_width=True):
            st.session_state.cached_plays = None
            st.rerun()
    with n3:
        st.success(f"Last refresh: {st.session_state.last_refresh.strftime('%H:%M:%S ET')}")

    # Header
    st.markdown(f"<h2 style='text-align:center;'>{st.session_state.away} {st.session_state.away_score} - {st.session_state.home_score} {st.session_state.home}</h2>", unsafe_allow_html=True)
    
    # Filters
    st.divider()
    with st.expander("🛠️ Play Filters", expanded=True):
        USE_PERIOD_FILTER = st.checkbox("🏒 Filter by Period", key="f_period", on_change=reset_filter_state)
        selected_periods = st.multiselect("Select Periods", options=sorted(list({p["period_label"] for p in plays})), key="f_period_sel") if USE_PERIOD_FILTER else []
        
        USE_GOAL_FILTER = st.checkbox("🚨 Goals Only", key="f_goal", on_change=reset_filter_state)
        USE_PP_FILTER   = st.checkbox("⚡ Power Plays Only", key="f_pp", on_change=reset_filter_state)
        USE_GP_FILTER   = st.checkbox("🥅 Empty Nets Only", key="f_en", on_change=reset_filter_state)

        b1, b2, _ = st.columns([1.5, 1.5, 7])
        with b1:
            if st.button("🚀 Apply Filters", use_container_width=True):
                def passes(p):
                    if USE_PERIOD_FILTER and selected_periods and p["period_label"] not in selected_periods: return False
                    if USE_GOAL_FILTER and p["type_text"] != "Goal": return False
                    if USE_PP_FILTER and "PP" not in p["situation"]: return False
                    if USE_GP_FILTER and "EN" not in p["situation"]: return False
                    return True
                st.session_state.filtered_plays = [p for p in plays if passes(p)]
                st.session_state.filters_applied = True
                st.rerun()
        with b2:
            if st.button("🗑️ Remove Filters", use_container_width=True):
                reset_filter_state()
                st.rerun()

    # Filter Results Header
    filters_active = st.session_state.get("filters_applied", False)
    display_list = st.session_state.filtered_plays if filters_active else plays
    
    if filters_active:
        showing, total = len(display_list), len(plays)
        if showing == 0: st.warning("No plays match these filters.")
        if USE_PERIOD_FILTER: st.info(f"🏒 Showing **{showing}** of **{total}** plays for selected periods.")
        if USE_GOAL_FILTER: st.info(f"🚨 Showing **{showing}** goals.")
        if USE_PP_FILTER: st.info(f"⚡ Showing **{showing}** Power Play events.")
        if USE_GP_FILTER: st.info(f"🥅 Showing **{showing}** Empty Net events.")

    # Render
    for p in display_list:
        with st.container():
            c1, c2 = st.columns([1, 5])
            c1.markdown(f"### {p['emoji']}")
            c2.markdown(f"**{p['period_label']} | {p['clock']}** - {p['situation']}")
            st.write(p['text'])
            st.caption(f"Score: {p['away_score']}-{p['home_score']} | {p['wall_et']}")
            st.divider()

else:
    # Schedule View
    d = st.date_input("Select Date", value=st.session_state.sched_date)
    st.session_state.sched_date = d
    games = fetch_scoreboard(d.strftime("%Y-%m-%d"))
    
    if not games: st.info("No games scheduled.")
    cols = st.columns(2)
    for i, g in enumerate(games):
        with cols[i % 2]:
            with st.container(border=True):
                st.markdown(f"""
                <div class="sched-team-row"><img src="{g['away_logo']}" width="30"/><span class="sched-team-name">{g['away_abbr']}</span><span class="sched-score">{g['away_score'] if g['has_score'] else ''}</span></div>
                <div class="sched-team-row"><img src="{g['home_logo']}" width="30"/><span class="sched-team-name">{g['home_abbr']}</span><span class="sched-score">{g['home_score'] if g['has_score'] else ''}</span></div>
                <div class="sched-meta">{g['time_str']} · {g['state_name']}</div>
                """, unsafe_allow_html=True)
                if st.button(f"View {g['away_abbr']} @ {g['home_abbr']}", key=g['event_id']):
                    st.session_state.event_id = g['event_id']
                    st.session_state.away, st.session_state.home = g['away_abbr'], g['home_abbr']
                    st.session_state.away_logo, st.session_state.home_logo = g['away_logo'], g['home_logo']
                    st.session_state.away_score, st.session_state.home_score = g['away_score'], g['home_score']
                    st.session_state.nhl_game_id = find_nhl_game_id(g['date_str'], g['away_abbr'], g['home_abbr'])
                    st.session_state.view = "game"
                    st.session_state.cached_plays = None
                    st.rerun()
