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
    "away": "", "home": "",
    "away_logo": "", "home_logo": "",
    "away_score": None, "home_score": None,
    "game_state": "",
    "filters_applied": False,
    "filtered_plays": None,
    "cached_plays": None,
    "cached_event_id": None,
    "last_refresh": datetime.now(ET), # Initialized to show on launch
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
    """
    Fetches and parses play-by-play data from ESPN with smart detection
    for Power Plays (PP) and Empty Net (EN) situations based on the 
    4-digit situationCode: [AwayGoalie][AwaySkaters][HomeSkaters][HomeGolie]
    """
    # Update timestamp whenever data is fetched
    st.session_state.last_refresh = datetime.now(ET)
    
    # Return cached data if available for this event
    if st.session_state.cached_event_id == event_id and st.session_state.cached_plays:
        return st.session_state.cached_plays
    
    try:
        resp = requests.get(ESPN_SUMMARY, params={"event": event_id}, timeout=15)
        resp.raise_for_status()
        raw_plays = resp.json().get("plays", [])
    except Exception as e:
        st.error(f"Error fetching game data: {e}")
        return []

    plays = []
    for p in raw_plays:
        text = p.get("text", "")
        sit = p.get("situation", {})
        # The 4-digit code is the most reliable way to detect goalie status and skaters
        sit_code = str(sit.get("situationCode", ""))
        
        # Default starting state
        away_s, home_s = 5, 5
        away_g_in, home_g_in = True, True

        # 1. PRIMARY LOGIC: Parse the 4-digit situationCode
        # Digits: 0=Away Goalie, 1=Away Skaters, 2=Home Skaters, 3=Home Goalie
        if len(sit_code) == 4:
            away_g_in = (sit_code[0] == '1')
            away_s    = int(sit_code[1])
            home_s    = int(sit_code[2])
            home_g_in = (sit_code[3] == '1')
        
        # 2. SECONDARY LOGIC: Fallback to individual fields if Code is missing
        elif sit.get("awaySkaters") is not None:
            away_s = sit.get("awaySkaters")
            home_s = sit.get("homeSkaters")

        # 3. CONSTRUCT STRENGTH LABEL
        # This fixes the issue where 0651 showed as 5v5 in image_e97e81.png
        strength_label = f"{away_s}v{home_s}"
        
        lower_text = text.lower()
        is_pp_keyword = "power play" in lower_text or "ppg" in lower_text
        
        if not away_g_in:
            strength_label += " (Away EN)"
        elif not home_g_in:
            strength_label += " (Home EN)"
        # If skaters are uneven OR it's a known PPG but goalie is in, mark as PP
        elif away_s != home_s or is_pp_keyword:
            strength_label += " (PP)"

        plays.append({
            "seq": int(p.get("sequenceNumber", 0)),
            "period_label": period_label(
                p.get("period", {}).get("number", 1), 
                p.get("period", {}).get("type", "")
            ),
            "clock": p.get("clock", {}).get("displayValue", ""),
            "type_text": p.get("type", {}).get("text", ""),
            "text": text,
            "strength": strength_label,
            "away_skaters": away_s,
            "home_skaters": home_s,
            "away_g_in": away_g_in,
            "home_g_in": home_g_in,
            "wall_et": fmt_et_full(p.get("wallclock", "")),
            "away_score": p.get("awayScore", 0),
            "home_score": p.get("homeScore", 0),
            "emoji": get_play_emoji(text),
        })
    
    # Sort by sequence to ensure chronological feed
    plays.sort(key=lambda x: x["seq"])
    
    # Cache results
    st.session_state.cached_plays = plays
    st.session_state.cached_event_id = event_id
    
    return plays
    
# ======================================================
# GAME FEED VIEW
# ======================================================
if st.session_state.view == "game":
    # 1. Navigation & Refresh Bar
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
        # Last refresh is now guaranteed to have a value from session_state init
        refresh_time = st.session_state.last_refresh.strftime("%H:%M:%S ET")
        st.markdown(f'''
            <div style="background-color:#2e7d32;color:white;padding:8px 16px;border-radius:4px;font-size:14px;font-weight:bold;text-align:center;">
                Last refresh {refresh_time}
            </div>
        ''', unsafe_allow_html=True)
            
    # 3. Header Scoreboard
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

    # 4. Filter Section
    raw_periods = list({p["period_label"] for p in plays})
    def p_key(l):
        if l.startswith('P'): return int(l[1:])
        if l == 'OT': return 100
        if l.startswith('OT'): return 100 + int(l[2:])
        return 200
    all_periods = sorted(raw_periods, key=p_key)

    USE_PERIOD_FILTER = st.checkbox("🏒 Filter by Period", value=False)
    selected_periods = st.multiselect("Select Periods", options=all_periods) if USE_PERIOD_FILTER else []
    USE_GOAL_FILTER = st.checkbox("🚨 Goals Only", value=False)
    USE_PP_FILTER = st.checkbox("🏒 Power Plays", value=False)
    USE_GP_FILTER = st.checkbox("🥅 Goalie Pulled", value=False)

    if st.button("🚀 Apply Filters"):
        def passes(p):
            a_s, h_s = p.get("away_skaters", 5), p.get("home_skaters", 5)
            a_g, h_g = p.get("away_g_in", True), p.get("home_g_in", True)
            if USE_PERIOD_FILTER and selected_periods and p["period_label"] not in selected_periods: return False
            if USE_GOAL_FILTER and p["type_text"] != "Goal": return False
            if USE_PP_FILTER and (a_s == h_s or not a_g or not h_g): return False
            if USE_GP_FILTER and (a_g and h_g): return False
            return True
        st.session_state.filtered_plays = [p for p in plays if passes(p)]
        st.session_state.filters_applied = True
        st.rerun()
        
    display_list = st.session_state.filtered_plays if st.session_state.get("filters_applied") else plays
    st.info(f"Showing {len(display_list)} of {len(plays)} plays.")

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
                    tooltip = "" if has_started else "Data will be available once the game starts."
                    if st.button(btn_label, key=f"btn_{g['event_id']}", use_container_width=True, disabled=not has_started, help=tooltip):
                        st.session_state.update({
                            "view": "game", "event_id": g["event_id"],
                            "away": g["away_abbr"], "home": g["home_abbr"],
                            "away_logo": g["away_logo"], "home_logo": g["home_logo"],
                            "away_score": g["away_score"], "home_score": g["home_score"],
                            "game_state": g["state"], "filters_applied": False,
                            "filtered_plays": None, "cached_plays": None, "cached_event_id": None
                        })
                        st.rerun()
