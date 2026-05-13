import streamlit as st
import requests
from datetime import datetime, date as ddate, timedelta, time as dtime
from zoneinfo import ZoneInfo

# =========================
# PAGE CONFIG & TITLE
# =========================
st.set_page_config(page_title="NHL Live", page_icon="🏒", layout="wide")
st.title("🏒 NHL Dashboard")

# Monday-first calendar via JS locale override
st.components.v1.html("""
<script>
(function() {
    const orig = Intl.DateTimeFormat;
    Intl.DateTimeFormat = function(l, o) { return new orig('en-GB', o); };
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
    "last_refresh": None,
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
    st.session_state.last_refresh = datetime.now(ET)
    
    if st.session_state.cached_event_id == event_id and st.session_state.cached_plays:
        return st.session_state.cached_plays
    
    resp = requests.get(ESPN_SUMMARY, params={"event": event_id}, timeout=15)
    raw_plays = resp.json().get("plays", [])
    plays = []
    
    for p in raw_plays:
        # --- ROBUST SITUATION EXTRACTION ---
        sit = p.get("situation", {})
        # Fallback to 5 to avoid NoneType errors and empty filters
        away_s = sit.get("awaySkaters", 5)
        home_s = sit.get("homeSkaters", 5)
        
        # Calculate strength label for display
        strength_label = f"{away_s}v{home_s}"
        
        # Standard play data
        p_obj = p.get("period", {})
        t_obj = p.get("type", {})
        text = p.get("text", "")
        
        plays.append({
            "seq": int(p.get("sequenceNumber", 0)),
            "period_label": period_label(p_obj.get("number", 1), p_obj.get("type", "")),
            "clock": p.get("clock", {}).get("displayValue", ""),
            "type_text": t_obj.get("text", ""),
            "text": text,
            "strength": strength_label,      # Displayed in header
            "away_skaters": away_s,          # Used by filters
            "home_skaters": home_s,          # Used by filters
            "wall_et": fmt_et_full(p.get("wallclock", "")),
            "wall_dt": to_et(p.get("wallclock", "")),
            "away_score": p.get("awayScore", ""),
            "home_score": p.get("homeScore", ""),
            "emoji": get_play_emoji(text),
        })
    
    plays.sort(key=lambda x: x["seq"])
    st.session_state.cached_plays = plays
    st.session_state.cached_event_id = event_id
    return plays

# ======================================================
# GAME FEED VIEW
# ======================================================
if st.session_state.view == "game":
    # 1. Navigation & Refresh Bar
    nav_col1, nav_col2, nav_col3, _ = st.columns([1.3, 1, 1.3, 6.4])
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
        if st.session_state.last_refresh:
            st.markdown(f'''
                <div style="background-color:#2e7d32;color:white;padding:8px 16px;border-radius:4px;font-size:14px;font-weight:bold;white-space:nowrap;">
                    Last refresh {st.session_state.last_refresh.strftime("%H:%M:%S ET")}
                </div>
            ''', unsafe_allow_html=True)
            
    # 2. Fetch Data
    plays = get_parsed_plays(st.session_state.event_id)
    
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

    # Stacked Checkboxes
    USE_PERIOD_FILTER = st.checkbox("🏒 Filter by Period", value=False)
    if USE_PERIOD_FILTER:
        selected_periods = st.multiselect("Select Periods", options=all_periods)
    else:
        selected_periods = []

    USE_TIME_FILTER = st.checkbox("🕐 Filter by Actual Time (ET)", value=False)
    START_DT = END_DT = None
    if USE_TIME_FILTER:
        all_wall_dts = [p["wall_dt"] for p in plays if p["wall_dt"]]
        game_start = min(all_wall_dts) if all_wall_dts else datetime.now(ET)
        game_end   = max(all_wall_dts) if all_wall_dts else datetime.now(ET)
        
        col_t1, col_t2 = st.columns(2)
        with col_t1:
            s_date = st.date_input("Start date", value=game_start.date(), key="fs_date")
            s_time = st.time_input("Start time", value=game_start.time(), key="fs_time")
        with col_t2:
            e_date = st.date_input("End date", value=game_end.date(), key="fe_date")
            e_time = st.time_input("End time", value=game_end.time(), key="fe_time")
        
        START_DT = datetime.combine(s_date, s_time).replace(tzinfo=ET)
        END_DT   = datetime.combine(e_date, e_time).replace(tzinfo=ET)

    USE_GOAL_FILTER = st.checkbox("🚨 Goals Only", value=False)
    USE_PP_FILTER = st.checkbox("🏒 Power Plays (5v4/4v5/5v3/4v3)", value=False)
    USE_GP_FILTER = st.checkbox("🥅 Goalie Pulled (6v5/5v6)", value=False)

    if st.button("🚀 Apply Filters"):
        def passes(p):
            a_s = p.get("away_skaters", 5)
            h_s = p.get("home_skaters", 5)

            if USE_PERIOD_FILTER and selected_periods and p["period_label"] not in selected_periods:
                return False
            
            if USE_TIME_FILTER and START_DT and END_DT:
                if not p["wall_dt"] or not (START_DT <= p["wall_dt"] <= END_DT):
                    return False
            
            if USE_GOAL_FILTER and p["type_text"] != "Goal":
                return False
            
            # Updated Power Play: Skaters are uneven AND no one has 6 (pulled goalie)
            if USE_PP_FILTER:
                if a_s == h_s or a_s == 6 or h_s == 6:
                    return False
            
            # Updated Goalie Pulled: Either team has 6 skaters
            if USE_GP_FILTER:
                if a_s != 6 and h_s != 6:
                    return False
                    
            return True
            
        st.session_state.filtered_plays = [p for p in plays if passes(p)]
        st.session_state.filters_applied = True

    # 5. Display Feed
    display_list = st.session_state.filtered_plays if st.session_state.filters_applied else plays
    if st.session_state.filters_applied:
        st.info(f"Showing **{len(display_list)}** of **{len(plays)}** plays.")

    if not display_list:
        st.warning("No plays found for the selected filters.")
    else:
        for p in display_list:
            emoji = "🚨" if p["type_text"] == "Goal" else p["emoji"]
            
            # Use 5v5 as a fallback if data is missing
            strength = p.get("strength") or "5v5"
            
            # Header with Period | Clock | Strength Badge
            st.subheader(f"{emoji} {p['period_label']} | ⏱️ {p['clock']} | `{strength}`")
            
            st.markdown(f"🎯 **Event:** {p['type_text']}")
            st.markdown(f"📋 **Play:** {p['text']}")
            st.markdown(f"📊 **Score:** {p['away_score']} - {p['home_score']}")
            
            if p["wall_et"]:
                st.markdown(f"🕐 **Time (ET):** `{p['wall_et']}`")
            st.divider()

# ======================================================
# SCHEDULE VIEW
# ======================================================
else:
    def handle_date_change():
        st.session_state.sched_date = st.session_state.calendar_widget

    date = st.date_input("Select date", value=st.session_state.sched_date, key="calendar_widget", on_change=handle_date_change)
    games = fetch_scoreboard(date.strftime("%Y-%m-%d"))

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
        st.info(f"No games scheduled for {date.strftime('%Y-%m-%d')}.")
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
                        st.session_state.update({
                            "view": "game", "event_id": g["event_id"],
                            "away": g["away_abbr"], "home": g["home_abbr"],
                            "away_logo": g["away_logo"], "home_logo": g["home_logo"],
                            "away_score": g["away_score"], "home_score": g["home_score"],
                            "game_state": g["state"], "filters_applied": False,
                            "filtered_plays": None, "cached_plays": None, "cached_event_id": None
                        })
                        st.rerun()
