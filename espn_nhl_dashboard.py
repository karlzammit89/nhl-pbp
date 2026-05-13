import streamlit as st
import requests
from datetime import datetime, date as ddate, timedelta, time as dtime
from zoneinfo import ZoneInfo

# =========================
# PAGE CONFIG & TITLE
# =========================
st.set_page_config(page_title="NHL Live", page_icon="🏒", layout="wide")
st.title("🏒 NHL Dashboard")

# Monday-first calendar via JS locale override with en-CA for YYYY-MM-DD
st.components.v1.html("""
<script>
(function() {
    const orig = Intl.DateTimeFormat;
    Intl.DateTimeFormat = function(l, o) { 
        return new orig('en-CA', o); 
    };
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
NHL_PBP_BASE    = "https://api-web.nhle.com/v1/gamecenter"

PLAY_EMOJI = {
    "goal": "🚨", "penalty": "🟡", "shot-on-goal": "🎯",
    "blocked-shot": "🛡️", "missed-shot": "🤦", "faceoff": "🏒",
    "hit": "💥", "giveaway": "❌", "takeaway": "⛹️",
    "stoppage": "⏸️", "period-start": "▶️", "period-end": "⏹️",
}

# =========================
# SESSION STATE INIT
# =========================
for k, v in {
    "view": "schedule",
    "event_id": None,
    "nhl_id": None,
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
def to_et(raw: str):
    if not raw: return None
    try: return datetime.fromisoformat(raw.replace("Z", "+00:00")).astimezone(ET)
    except: return None

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

def period_label(period_num, period_type: str = "regulation") -> str:
    if "overtime" in (period_type or "").lower() or period_num > 3:
        ot_num = period_num - 3
        return f"OT{ot_num}" if ot_num > 1 else "OT"
    if "shootout" in (period_type or "").lower(): return "SO"
    return f"P{period_num}"

# =========================
# HYBRID MAPPING HELPERS
# =========================
@st.cache_data(ttl=300)
def get_nhl_game_id(date_str, away_abbr, home_abbr):
    """Maps ESPN game to NHL API game ID."""
    try:
        resp = requests.get(f"{NHL_SCHEDULE}/{date_str}", timeout=10).json()
        for week_day in resp.get("gameWeek", []):
            if week_day.get("date") == date_str:
                for game in week_day.get("games", []):
                    # Match by team abbreviations
                    nhl_away = game.get("awayTeam", {}).get("abbrev")
                    nhl_home = game.get("homeTeam", {}).get("abbrev")
                    if nhl_away == away_abbr and nhl_home == home_abbr:
                        return game.get("id")
    except: pass
    return None

def get_strength_from_nhl(e_period, e_clock, nhl_plays):
    """Fuzzy logic to find NHL strength code for an ESPN play."""
    if not nhl_plays:
        return "5v5", 5, 5, True, True

    def to_sec(ts):
        try:
            m, s = map(int, ts.split(':'))
            return m * 60 + s
        except: return 0

    e_sec = to_sec(e_clock)
    best_match = None
    
    # Try direct match, then +/- 2 seconds fuzzy match
    for np in nhl_plays:
        if np.get("period") == e_period:
            n_sec = to_sec(np.get("timeInPeriod", "00:00"))
            if abs(e_sec - n_sec) <= 2:
                best_match = np
                if e_sec == n_sec: break

    if best_match:
        sc = str(best_match.get("situationCode", "1551"))
        if len(sc) == 4:
            a_g, a_s, h_s, h_g = sc[0], sc[1], sc[2], sc[3]
            label = f"{a_s}v{h_s}"
            if a_g == '0': label += " (Empty Net)"
            elif h_g == '0': label += " (Empty Net)"
            return label, int(a_s), int(h_s), a_g == '1', h_g == '1'
            
    return "5v5", 5, 5, True, True

# =========================
# CACHED API CALLS
# =========================
@st.cache_data(ttl=30, show_spinner=False)
def fetch_scoreboard(date_str: str) -> list:
    date_compact = date_str.replace("-", "")
    resp = requests.get(ESPN_SCOREBOARD, params={"dates": date_compact, "limit": 25}, timeout=10)
    data = resp.json()
    games = []
    for event in data.get("events", []):
        comp = event.get("competitions", [{}])[0]
        status = comp.get("status", {})
        state_type = status.get("type", {})
        state = state_type.get("state", "pre")
        raw_name = state_type.get("name", "")
        display_status = "Scheduled" if raw_name == "STATUS_SCHEDULED" else state_type.get("shortDetail", "")
        competitors = comp.get("competitors", [])
        away = next(c for c in competitors if c.get("homeAway") == "away")
        home = next(c for c in competitors if c.get("homeAway") == "home")
        start_dt = to_et(event.get("date", ""))
        is_final, is_live = (state == "post"), (state == "in")
        
        games.append({
            "event_id": event.get("id", ""),
            "state": state,
            "state_name": display_status,
            "is_live": is_live, "is_final": is_final,
            "is_ot": status.get("period", 0) > 3,
            "away_abbr": away.get("team", {}).get("abbreviation", "?"),
            "home_abbr": home.get("team", {}).get("abbreviation", "?"),
            "away_logo": away.get("team", {}).get("logo", ""),
            "home_logo": home.get("team", {}).get("logo", ""),
            "away_score": int(away.get("score", 0) or 0),
            "home_score": int(home.get("score", 0) or 0),
            "has_score": is_live or is_final,
            "time_str": fmt_game_time(start_dt),
        })
    return sorted(games, key=lambda x: x["time_str"])

def get_parsed_plays(event_id: str) -> list:
    st.session_state.last_refresh = datetime.now(ET)
    if st.session_state.cached_event_id == event_id and st.session_state.cached_plays:
        return st.session_state.cached_plays
    
    # 1. Fetch ESPN Data (The Base)
    resp = requests.get(ESPN_SUMMARY, params={"event": event_id}, timeout=15)
    raw_espn_plays = resp.json().get("plays", [])
    
    # 2. Fetch NHL Data (The Strength Source)
    nhl_plays = []
    if st.session_state.nhl_id:
        try:
            n_resp = requests.get(f"{NHL_PBP_BASE}/{st.session_state.nhl_id}/play-by-play", timeout=10)
            nhl_plays = n_resp.json().get("plays", [])
        except: pass

    plays = []
    for p in raw_espn_plays:
        e_period = p.get("period", {}).get("number", 1)
        e_clock = p.get("clock", {}).get("displayValue", "00:00")
        
        # HYBRID LOGIC: Map ESPN play to NHL strength
        strength_label, a_s, h_s, a_g, h_g = get_strength_from_nhl(e_period, e_clock, nhl_plays)
        
        plays.append({
            "seq": int(p.get("sequenceNumber", 0)),
            "period_label": period_label(e_period, p.get("period", {}).get("type", "")),
            "clock": e_clock,
            "type_text": p.get("type", {}).get("text", ""),
            "text": p.get("text", ""),
            "strength": strength_label,
            "away_skaters": a_s,
            "home_skaters": h_s,
            "away_g_in": a_g,
            "home_g_in": h_g,
            "wall_et": fmt_et_full(p.get("wallclock", "")),
            "wall_dt": to_et(p.get("wallclock", "")),
            "away_score": p.get("awayScore", ""),
            "home_score": p.get("homeScore", ""),
            "emoji": get_play_emoji(p.get("text", "")),
        })
    
    plays.sort(key=lambda x: x["seq"])
    st.session_state.cached_plays = plays
    st.session_state.cached_event_id = event_id
    return plays

# ======================================================
# GAME FEED VIEW
# ======================================================
if st.session_state.view == "game":
    plays = get_parsed_plays(st.session_state.event_id)
    
    nav_col1, nav_col2, nav_col3, _ = st.columns([1.3, 1, 1.8, 5.9])
    with nav_col1:
        if st.button("⬅ Back to Schedule", use_container_width=True):
            st.session_state.view = "schedule"
            st.session_state.filters_applied = False
            st.session_state.filtered_plays = None
            st.rerun()
    with nav_col2:
        if st.button("🔄 Refresh", use_container_width=True):
            st.session_state.cached_plays = None
            st.rerun()
    with nav_col3:
        refresh_time = st.session_state.last_refresh.strftime("%H:%M:%S ET")
        st.markdown(f'''
            <div style="background-color:#2e7d32;color:white;padding:8px 16px;border-radius:4px;font-size:14px;font-weight:bold;text-align:center;">
                Last refresh {refresh_time}
            </div>
        ''', unsafe_allow_html=True)
            
    st.markdown("<br>", unsafe_allow_html=True)
    head_c1, head_c2, head_c3 = st.columns([1, 6, 1])
    with head_c1: 
        st.image(st.session_state.away_logo, width=80)
    with head_c2:
        st.markdown(f"""
            <div style="display:flex;align-items:center;justify-content:center;font-weight:800;font-size:clamp(20px,3vw,32px);gap:15px;text-align:center;">
                <span>{st.session_state.away}</span>
                <span style="color:#888;">{st.session_state.away_score}</span>
                <span style="color:#444;">-</span>
                <span style="color:#888;">{st.session_state.home_score}</span>
                <span>{st.session_state.home}</span>
            </div>
        """, unsafe_allow_html=True)
    with head_c3: 
        st.image(st.session_state.home_logo, width=80)
    st.divider()

    # Filters
    raw_periods = list({p["period_label"] for p in plays})
    def p_key(l):
        if l.startswith('P'): return int(l[1:])
        if l == 'OT': return 100
        if l.startswith('OT'): return 100 + int(l[2:])
        return 200
    all_periods = sorted(raw_periods, key=p_key)

    all_dts = [p["wall_dt"] for p in plays if p["wall_dt"]]
    game_start_default = min(all_dts) if all_dts else None
    game_end_default   = max(all_dts) if all_dts else None

    USE_PERIOD_FILTER = st.checkbox("🏒 Filter by Period", value=False)
    selected_periods = st.multiselect("Select Periods", options=all_periods) if USE_PERIOD_FILTER else []
    
    USE_TIME_FILTER = st.checkbox("🕐 Filter by Actual Time (ET)", value=False)
    START_DT = END_DT = None
    if USE_TIME_FILTER:
        def_start_date = game_start_default.date() if game_start_default else ddate.today()
        def_end_date   = game_end_default.date()   if game_end_default   else ddate.today()
        def_start_time = game_start_default.time() if game_start_default else dtime(19, 0)
        def_end_time   = game_end_default.time()   if game_end_default   else dtime(23, 59)
        st.markdown("**Start date/time (ET)**")
        sc1, sc2 = st.columns(2)
        with sc1: start_date_input = st.date_input("Start date", value=def_start_date, key="tf_start_date")
        with sc2: start_time_input = st.time_input("Start time", value=def_start_time, step=60, key="tf_start_time")
        st.markdown("**End date/time (ET)**")
        ec1, ec2 = st.columns(2)
        with ec1: end_date_input = st.date_input("End date", value=def_end_date, key="tf_end_date")
        with ec2: end_time_input = st.time_input("End time", value=def_end_time, step=60, key="tf_end_time")
        START_DT = datetime.combine(start_date_input, start_time_input).replace(tzinfo=ET)
        END_DT   = datetime.combine(end_date_input,   end_time_input).replace(tzinfo=ET)

    USE_GOAL_FILTER = st.checkbox("🚨 Goals Only", value=False)
    USE_PP_FILTER = st.checkbox("⚡ Power Plays Only", value=False)
    USE_GP_FILTER = st.checkbox("🥅 Empty Nets Only", value=False)

    if st.button("🚀 Apply Filters"):
        def passes(p):
            a_s, h_s = p.get("away_skaters", 5), p.get("home_skaters", 5)
            a_g, h_g = p.get("away_g_in", True), p.get("home_g_in", True)
            if USE_PERIOD_FILTER and selected_periods and p["period_label"] not in selected_periods: return False
            if USE_TIME_FILTER:
                if not p["wall_dt"] or START_DT is None or END_DT is None: return False
                if not (START_DT <= p["wall_dt"] <= END_DT): return False
            if USE_GOAL_FILTER and p["type_text"] != "Goal": return False
            if USE_PP_FILTER and (a_s == h_s): return False # Only true strength diff
            if USE_GP_FILTER and (a_g and h_g): return False
            return True
        st.session_state.filtered_plays = [p for p in plays if passes(p)]
        st.session_state.filters_applied = True
        st.rerun()
        
    filters_applied = st.session_state.get("filters_applied")
    display_list = st.session_state.filtered_plays if filters_applied else plays
    total, showing = len(plays), len(display_list)

    if filters_applied and showing == 0:
        st.warning("⚠️ No results found — please check the filters applied.")
        st.stop()

    # Render plays
    for p in display_list:
        emoji = "🚨" if p.get("type_text") == "Goal" else p.get("emoji", "🏒")
        st.subheader(f"{emoji} {p.get('period_label')} | ⏱️ {p.get('clock')}")
        st.markdown(f"📊 **Score:** {p.get('away_score')} - {p.get('home_score')}")
        st.markdown(f"🎯 **Event:** {p.get('type_text')}")
        st.markdown(f"⚖️ **Strength:** `{p.get('strength', '5v5')}`")
        st.markdown(f"📋 **Play:** {p.get('text')}")
        if p.get("wall_et"):
            st.markdown(f"🕐 **Time (ET):** `{p['wall_et']}`")
        st.divider()

# ======================================================
# SCHEDULE VIEW
# ======================================================
else:
    def handle_date_change():
        st.session_state.sched_date = st.session_state.calendar_widget

    date = st.date_input("Select date", value=st.session_state.sched_date, key="calendar_widget", on_change=handle_date_change)
    formatted_date = date.strftime("%Y-%m-%d")
    games = fetch_scoreboard(formatted_date)

    st.markdown("""
        <style>
            .sched-team-row { display: flex; align-items: center; gap: 12px; margin-bottom: 6px; }
            .sched-team-name { font-size: 22px; font-weight: 800; color: #ffffff; }
            .sched-score { font-size: 22px; font-weight: 800; color: #888888; margin-left: auto; }
            .sched-meta { font-size: 13px; color: #999999; border-top: 1px solid rgba(255,255,255,0.1); padding-top: 8px; margin-top: 8px; display: flex; align-items: center; }
            .sched-extra { background: #e67e22; color: #fff; font-size: 11px; padding: 2px 6px; border-radius: 4px; margin-left: 8px; font-weight: bold; }
        </style>
    """, unsafe_allow_html=True)

    if not games:
        st.info(f"No games scheduled for {formatted_date}.")
    else:
        cols = st.columns(2)
        for i, g in enumerate(games):
            has_started = g["has_score"]
            ot_badge = f'<span class="sched-extra">OT</span>' if g["is_ot"] else ""
            card_html = f"""
            <div class="sched-team-row">
                <img src="{g['away_logo']}" width="34"/>
                <span class="sched-team-name">{g['away_abbr']}</span>
                <span class="sched-score">{g['away_score'] if has_started else ''}</span>
            </div>
            <div class="sched-team-row">
                <img src="{g['home_logo']}" width="34"/>
                <span class="sched-team-name">{g['home_abbr']}</span>
                <span class="sched-score">{g['home_score'] if has_started else ''}</span>
            </div>
            <div class="sched-meta">{g['time_str']} &middot; {g['state_name']}{ot_badge}</div>
            """
            with cols[i % 2]:
                with st.container(border=True):
                    st.markdown(card_html, unsafe_allow_html=True)
                    btn_label = f"▶ Open {g['away_abbr']} @ {g['home_abbr']}" if has_started else "⏳ Not Started"
                    if st.button(btn_label, key=f"btn_{g['event_id']}", use_container_width=True, disabled=not has_started):
                        # Map NHL ID on selection
                        nhl_id = get_nhl_game_id(formatted_date, g['away_abbr'], g['home_abbr'])
                        st.session_state.update({
                            "view": "game", "event_id": g["event_id"], "nhl_id": nhl_id,
                            "away": g["away_abbr"], "home": g["home_abbr"],
                            "away_logo": g["away_logo"], "home_logo": g["home_logo"],
                            "away_score": g["away_score"], "home_score": g["home_score"],
                            "game_state": g["state"], "filters_applied": False,
                            "filtered_plays": None, "cached_plays": None, "cached_event_id": None
                        })
                        st.rerun()
