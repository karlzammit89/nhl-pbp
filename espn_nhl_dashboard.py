import streamlit as st
import requests
from datetime import datetime, date as ddate, timedelta
from zoneinfo import ZoneInfo

# =========================
# PAGE CONFIG & TITLE
# =========================
st.set_game_config = st.set_page_config(page_title="NHL Live", page_icon="🏒", layout="wide")
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
# API CALLS & ROBUST LOGIC
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
        state = status.get("type", {}).get("state", "pre")
        display_status = status.get("type", {}).get("shortDetail", "Scheduled")
        
        competitors = comp.get("competitors", [])
        away = next(c for c in competitors if c.get("homeAway") == "away")
        home = next(c for c in competitors if c.get("homeAway") == "home")
        
        games.append({
            "event_id": event.get("id", ""),
            "state_name": display_status,
            "is_live": (state == "in"),
            "is_final": (state == "post"),
            "is_ot": status.get("period", 0) > 3,
            "away_abbr": away.get("team", {}).get("abbreviation", "?"),
            "home_abbr": home.get("team", {}).get("abbreviation", "?"),
            "away_logo": away.get("team", {}).get("logo", ""),
            "home_logo": home.get("team", {}).get("logo", ""),
            "away_score": int(away.get("score", 0) or 0),
            "home_score": int(home.get("score", 0) or 0),
            "has_score": (state != "pre"),
            "time_str": fmt_game_time(to_et(event.get("date", ""))),
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
        text = p.get("text", "")
        type_text = (p.get("type", {}).get("text", "") or "").upper()
        sit = p.get("situation", {})
        sit_code = str(sit.get("situationCode", ""))
        
        # --- ROBUST LOGIC ---
        away_s, home_s = 5, 5
        away_g_in, home_g_in = True, True
        if len(sit_code) == 4:
            away_g_in, away_s, home_s, home_g_in = (sit_code[0] == '1'), int(sit_code[1]), int(sit_code[2]), (sit_code[3] == '1')

        l_text = text.lower()
        is_pp = type_text in ["PPG", "SHG", "POWER PLAY GOAL"] or "power play" in l_text
        is_en = type_text == "ENG" or "empty net" in l_text or not (away_g_in and home_g_in)

        tags = []
        if is_pp:
            if away_s == home_s:
                if any(x in l_text for x in [st.session_state.away.lower(), "away"]): tags.append(f"{st.session_state.away} PP")
                else: tags.append(f"{st.session_state.home} PP")
            else:
                tags.append(f"{st.session_state.away if away_s > home_s else st.session_state.home} PP")
        
        if is_en:
            tags.append(f"{st.session_state.away if (not away_g_in or 'away' in l_text) else st.session_state.home} EN")

        adj_away, adj_home = away_s + (0 if away_g_in else 1), home_s + (0 if home_g_in else 1)
        strength_str = f"{adj_away}v{adj_home}" + (" (" + ", ".join(sorted(set(tags))) + ")" if tags else "")

        plays.append({
            "seq": int(p.get("sequenceNumber", 0)),
            "period_label": period_label(p.get("period", {}).get("number", 1), p.get("period", {}).get("type", "")),
            "clock": p.get("clock", {}).get("displayValue", ""),
            "type_text": type_text,
            "text": text,
            "strength": strength_str,
            "is_pp": is_pp or (away_s != home_s),
            "is_en": is_en,
            "wall_et": fmt_et_full(p.get("wallclock", "")),
            "away_score": p.get("awayScore", ""),
            "home_score": p.get("homeScore", ""),
            "emoji": get_play_emoji(text),
        })
    
    # SORTED OLDEST TO NEWEST (TOP TO BOTTOM)
    plays.sort(key=lambda x: x["seq"])
    st.session_state.cached_plays = plays
    st.session_state.cached_event_id = event_id
    return plays

# ======================================================
# GAME VIEW
# ======================================================
if st.session_state.view == "game":
    plays = get_parsed_plays(st.session_state.event_id)
    
    nav_col1, nav_col2, nav_col3, _ = st.columns([1.3, 1, 1.8, 5.9])
    with nav_col1:
        if st.button("⬅ Back to Schedule", use_container_width=True):
            st.session_state.view = "schedule"
            st.rerun()
    with nav_col2:
        if st.button("🔄 Refresh", use_container_width=True):
            st.session_state.cached_plays = None
            st.rerun()
    with nav_col3:
        st.markdown(f'''<div style="background-color:#2e7d32;color:white;padding:8px;border-radius:4px;font-size:14px;font-weight:bold;text-align:center;">Last refresh {st.session_state.last_refresh.strftime("%H:%M:%S ET")}</div>''', unsafe_allow_html=True)
            
    st.markdown("<br>", unsafe_allow_html=True)
    h1, h2, h3 = st.columns([1, 6, 1])
    with h1: st.image(st.session_state.away_logo, width=80)
    with h2: st.markdown(f"""<div style="display:flex;align-items:center;justify-content:center;font-weight:800;font-size:32px;gap:15px;"><span>{st.session_state.away}</span><span style="color:#888;">{st.session_state.away_score}</span><span style="color:#444;">-</span><span style="color:#888;">{st.session_state.home_score}</span><span>{st.session_state.home}</span></div>""", unsafe_allow_html=True)
    with h3: st.image(st.session_state.home_logo, width=80)
    st.divider()

    # Filter UI
    raw_periods = list({p["period_label"] for p in plays})
    def p_key(l): return int(l[1:]) if l.startswith('P') else (100 if l == 'OT' else 200)
    all_periods = sorted(raw_periods, key=p_key)

    USE_PERIOD_FILTER = st.checkbox("🏒 Filter by Period", value=False)
    selected_periods = st.multiselect("Select Periods", options=all_periods) if USE_PERIOD_FILTER else []
    USE_GOAL_FILTER = st.checkbox("🚨 Goals Only", value=False)
    USE_PP_FILTER = st.checkbox("🏒 Power Plays Only", value=False)
    USE_GP_FILTER = st.checkbox("🥅 Empty Net/Goalie Pulled", value=False)

    if st.button("🚀 Apply Filters"):
        def passes(p):
            if USE_PERIOD_FILTER and selected_periods and p["period_label"] not in selected_periods: return False
            if USE_GOAL_FILTER and "GOAL" not in p["type_text"]: return False
            if USE_PP_FILTER and not p["is_pp"]: return False
            if USE_GP_FILTER and not p["is_en"]: return False
            return True
        st.session_state.filtered_plays = [p for p in plays if passes(p)]
        st.session_state.filters_applied = True
        st.rerun()
        
    display_list = st.session_state.filtered_plays if st.session_state.filters_applied else plays
    st.info(f"Showing {len(display_list)} plays.")

    for p in display_list:
        emoji = "🚨" if "GOAL" in p["type_text"] else p["emoji"]
        st.subheader(f"{emoji} {p['period_label']} | ⏱️ {p['clock']}")
        st.markdown(f"📊 **Score:** {p['away_score']} - {p['home_score']}")
        st.markdown(f"🎯 **Event:** {p['type_text']}")
        st.markdown(f"⚖️ **Strength:** `{p['strength']}`")
        st.markdown(f"📋 **Play:** {p['text']}")
        if p["wall_et"]: st.markdown(f"🕐 **Time (ET):** `{p['wall_et']}`")
        st.divider()

# ======================================================
# SCHEDULE VIEW
# ======================================================
else:
    date = st.date_input("Select date", value=st.session_state.sched_date)
    st.session_state.sched_date = date
    games = fetch_scoreboard(date.strftime("%Y-%m-%d"))

    st.markdown("""<style>.sched-team-row { display: flex; align-items: center; gap: 12px; margin-bottom: 6px; }.sched-team-name { font-size: 22px; font-weight: 800; color: #ffffff; }.sched-score { font-size: 22px; font-weight: 800; color: #888888; margin-left: auto; }.sched-meta { font-size: 13px; color: #999999; border-top: 1px solid rgba(255,255,255,0.1); padding-top: 8px; margin-top: 8px; display: flex; align-items: center; }.sched-extra { background: #e67e22; color: #fff; font-size: 11px; padding: 2px 6px; border-radius: 4px; margin-left: 8px; font-weight: bold; }</style>""", unsafe_allow_html=True)

    if not games:
        st.info("No games scheduled.")
    else:
        cols = st.columns(2)
        for i, g in enumerate(games):
            has_started = g["has_score"]
            ot_badge = f'<span class="sched-extra">OT</span>' if g["is_ot"] else ""
            card_html = f"""<div class="sched-team-row"><img src="{g['away_logo']}" width="34"/><span class="sched-team-name">{g['away_abbr']}</span><span class="sched-score">{g['away_score'] if has_started else ''}</span></div><div class="sched-team-row"><img src="{g['home_logo']}" width="34"/><span class="sched-team-name">{g['home_abbr']}</span><span class="sched-score">{g['home_score'] if has_started else ''}</span></div><div class="sched-meta">{g['time_str']} &middot; {g['state_name']}{ot_badge}</div>"""
            with cols[i % 2]:
                with st.container(border=True):
                    st.markdown(card_html, unsafe_allow_html=True)
                    if st.button(f"▶ Open {g['away_abbr']} @ {g['home_abbr']}" if has_started else "⏳ Not Started", key=f"btn_{g['event_id']}", use_container_width=True, disabled=not has_started):
                        st.session_state.update({"view": "game", "event_id": g["event_id"], "away": g["away_abbr"], "home": g["home_abbr"], "away_logo": g["away_logo"], "home_logo": g["home_logo"], "away_score": g["away_score"], "home_score": g["home_score"], "filters_applied": False, "cached_plays": None})
                        st.rerun()
