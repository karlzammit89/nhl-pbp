import streamlit as st
import requests
import time
from datetime import datetime, date as ddate, timedelta, time as dtime
from zoneinfo import ZoneInfo

# =========================
# PAGE CONFIG & TITLE
# =========================
st.set_page_config(page_title="NHL Play by Play", page_icon="🏒", layout="wide")
st.title("🏒 NHL Play by Play")

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

# ESPN NHL type.id — confirmed penalty infraction IDs
# Sourced from four production scans: Oct-Dec 2025, Jan-Feb 2026, Apr-May 2026
# plus confirmed decisions on Abusive Language (101) and Abuse of Official minor (64).
# Only explicitly confirmed IDs are used — no threshold assumptions.
# Add new IDs here as they are confirmed via the game diagnostic expander.
#
# Confirmed NO-PP (never add): 25=Leaving Crease, 38/106=Instigator,
#   80=Fighting, 91=Misconduct, 93/94=Game/Abuse Misconduct,
#   105=Aggressor, 143=Penalty Shot, 509=Penalty (ambiguous)
#
# Still unknown (extremely rare): Spearing, Clipping, Head Butting, Biting, Spitting
ESPN_KNOWN_PENALTY_IDS = {
      7,   # Boarding (minor variant)
      9,   # Broken Stick
     10,   # Butt Ending
     11,   # Charging
     12,   # Closing Hand on Puck
     13,   # Cross checking
     17,   # Elbowing
     20,   # Illegal Check to the Head
     22,   # Delay of Game
     29,   # High-sticking
     30,   # High sticking (label variant of 29)
     31,   # Holding
     32,   # Holding the Stick
     33,   # Hooking
     35,   # Illegal Stick
     37,   # Interference
     39,   # Kneeing
     40,   # Goalkeeper Interference
     45,   # Roughing
     49,   # Slashing
     52,   # Throwing the Stick
     55,   # Tripping
     57,   # Unsportsmanlike Conduct
     58,   # Too Many Men on the Ice
     64,   # Abuse of Official (minor level)
     72,   # Boarding (major variant)
     76,   # Crosschecking (label variant of 13)
     85,   # Kneeing (variant ID)
     86,   # Slashing (variant ID)
    101,   # Abusive Language
    107,   # Delaying Game - Illegal Play by Goaltender
    108,   # Delaying Game - Smothering Puck
    109,   # Delaying Game - Puck over Glass
    123,   # Interference (variant ID)
    132,   # Throwing Object at Puck
    135,   # Hooking on Breakaway
    136,   # Tripping on Breakaway
    137,   # Slashing on Breakaway
    140,   # Game Misconduct - Head Coach (bench minor)
    142,   # Embellishment
    149,   # Removing Opponent Helmet
    150,   # Goalie Removed Own Mask
    151,   # Delay Game - Unsuccessful Challenge
    163,   # Playing without a Helmet
}

def is_espn_penalty(type_id) -> bool:
    """
    Returns True only if type_id is a confirmed ESPN penalty infraction ID.
    Deliberately conservative — unknown IDs return False until confirmed
    by running the diagnostic scanner across more games.
    """
    try:
        return int(type_id) in ESPN_KNOWN_PENALTY_IDS
    except (ValueError, TypeError):
        return False

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
    "active_filter_snapshot": {},
    "sort_newest_first": False,
    "force_bucket": None,
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
def to_et(raw):
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).astimezone(ET)
    except Exception:
        return None

def fmt_et_full(raw):
    dt = to_et(raw)
    if not dt:
        return "N/A"
    label = "EDT" if dt.dst() != timedelta(0) else "EST"
    return dt.strftime(f"%Y-%m-%d %H:%M:%S {label}")

def fmt_game_time(dt):
    return dt.strftime("%H:%M ET") if dt else "TBD"

def get_play_emoji(type_str):
    t = (type_str or "").lower()
    for k, v in PLAY_EMOJI.items():
        if k in t:
            return v
    return "🏒"

def period_label(period_num, period_type=""):
    pt = (period_type or "").lower()
    if "overtime" in pt or (isinstance(period_num, int) and period_num > 3):
        ot_num = period_num - 3
        return f"OT{ot_num}" if ot_num > 1 else "OT"
    if "shootout" in pt:
        return "SO"
    return f"P{period_num}"

def espn_clock_to_seconds(clock_str, period_num):
    """ESPN clock counts UP from 0:00 (elapsed)."""
    try:
        mm, ss = clock_str.strip().split(":")
        elapsed = int(mm) * 60 + int(ss)
    except Exception:
        elapsed = 0
    return (period_num - 1) * 1200 + elapsed

def nhl_clock_to_seconds(time_in_period, period_num):
    """NHL clock counts DOWN from 20:00 (remaining)."""
    try:
        mm, ss = time_in_period.strip().split(":")
        elapsed = 1200 - (int(mm) * 60 + int(ss))
    except Exception:
        elapsed = 0
    return (period_num - 1) * 1200 + elapsed

def parse_nhl_situation(sit_code):
    """
    Parse NHL situation code: [away_goalie][away_sk][home_sk][home_goalie]
    Returns e.g. '5v5', '4v5 PP', '6v5 Away EN PP'
    """
    if not sit_code or len(sit_code) < 4:
        return ""
    try:
        a = int(sit_code[1])
        h = int(sit_code[2])
    except ValueError:
        return ""
    away_en = sit_code[0] == "0"
    home_en = sit_code[3] == "0"
    is_pp   = a != h
    parts   = [f"{a}v{h}"]
    if away_en and home_en:
        parts.append("Both EN")
    elif away_en:
        parts.append("Away EN")
    elif home_en:
        parts.append("Home EN")
    if is_pp:
        parts.append("PP")
    return " ".join(parts)

# =========================
# NHL GAME ID LOOKUP
# =========================
@st.cache_data(ttl=3600, show_spinner=False)
def find_nhl_game_id(date_str, away_abbr, home_abbr):
    try:
        data = requests.get(f"{NHL_SCHEDULE}/{date_str}", timeout=10).json()
    except Exception:
        return ""
    target = datetime.fromisoformat(date_str).date()
    for day in data.get("gameWeek", []):
        for g in day.get("games", []):
            dt = to_et(g.get("startTimeUTC", ""))
            if dt and dt.date() != target:
                continue
            nhl_away = g.get("awayTeam", {}).get("abbrev", "").upper()
            nhl_home = g.get("homeTeam", {}).get("abbrev", "").upper()
            if _abbr_match(away_abbr, nhl_away) and _abbr_match(home_abbr, nhl_home):
                return str(g.get("id", ""))
    return ""

def _abbr_match(a, b):
    a, b = a.upper(), b.upper()
    if a == b:
        return True
    KNOWN = {
        "VGK": ["VEG","VGK","LV"], "VEG": ["VGK","VEG","LV"], "LV": ["VGK","VEG"],
        "UTA": ["UTAH","UTA"], "WSH": ["WAS","WSH"], "WAS": ["WSH","WAS"],
        "CLB": ["CBJ","CLB"], "CBJ": ["CLB","CBJ"],
    }
    return a in KNOWN.get(b, []) or b in KNOWN.get(a, [])

# =========================
# NHL PLAY-BY-PLAY FETCH
# =========================
@st.cache_data(show_spinner=False)
def fetch_nhl_plays(nhl_game_id, cache_bucket: int = 0):
    """
    Fetch NHL play-by-play.
    Returns:
      plays:          all plays (sit_code + type_key) for EN window detection
      delayed_events: delayed-penalty events for delayed card injection
      teams:          away/home IDs and abbreviations
    """
    if not nhl_game_id:
        return {"plays": [], "delayed_events": [], "teams": {}}
    try:
        resp = requests.get(f"{NHL_PBP}/{nhl_game_id}/play-by-play", timeout=10)
        resp.raise_for_status()
        data = resp.json()
    except Exception:
        return {"plays": [], "delayed_events": [], "teams": {}}

    teams = {
        "away_id":   data.get("awayTeam", {}).get("id"),
        "home_id":   data.get("homeTeam", {}).get("id"),
        "away_abbr": (data.get("awayTeam", {}).get("abbrev") or "").upper(),
        "home_abbr": (data.get("homeTeam", {}).get("abbrev") or "").upper(),
    }

    plays          = []
    delayed_events = []

    for p in data.get("plays", []):
        pd       = p.get("periodDescriptor", {})
        pnum     = pd.get("number", 1)
        tip      = p.get("timeInPeriod", "20:00")
        sit      = p.get("situationCode") or ""
        type_key = p.get("typeDescKey", "")
        elapsed  = nhl_clock_to_seconds(tip, pnum)

        plays.append({
            "period":     pnum,
            "elapsed":    elapsed,
            "type_key":   type_key,
            "sit_code":   sit,
            "sort_order": p.get("sortOrder", 0),
        })

        # Capture delayed-penalty events.
        # eventOwnerTeamId = drawing team (confirmed: 421 events, 160 games).
        if type_key == "delayed-penalty":
            det = p.get("details") or {}
            delayed_events.append({
                "elapsed":  elapsed,
                "period":   pnum,
                "owner_id": str(det.get("eventOwnerTeamId", "") or ""),
            })

    return {"plays": plays, "delayed_events": delayed_events, "teams": teams}


# =========================
# ESPN-FIRST PP PIPELINE
# =========================
# Infractions that create a PP (used to filter standalone misconducts)
_PP_INFRACTIONS = {
    "hooking","tripping","interference","roughing","slashing","cross-checking",
    "boarding","high-sticking","holding","delay","spearing","elbowing",
    "charging","butt-ending","closing","too-many","goaltender","unsportsmanlike",
}

def _build_bm(espn_json):
    """ESPN team.id → 'away'/'home' from boxscore + header."""
    bm = {}
    for t in ((espn_json.get("boxscore") or {}).get("teams", [])):
        te = t.get("team", {}) if isinstance(t.get("team"), dict) else {}
        tid = str(te.get("id", "")); ha = t.get("homeAway", "")
        if tid and ha: bm[tid] = ha
    for c in ((espn_json.get("header") or {}).get("competitions", [])):
        for comp in (c.get("competitors", [])):
            te  = comp.get("team", {}) if isinstance(comp.get("team"), dict) else {}
            tid = str(te.get("id", "")); ha = comp.get("homeAway", "")
            if tid and ha: bm[tid] = ha
    return bm

def _resolve_team(play, bm):
    """Penalised team abbreviation from ESPN penalty play."""
    to = play.get("team", {}) if isinstance(play.get("team"), dict) else {}
    ab = (to.get("abbreviation") or "").upper()
    return ab if ab else bm.get(str(to.get("id", "")), "")

def _creates_pp(text):
    """False for standalone misconducts (no PP). True otherwise."""
    t = text.lower()
    if "misconduct" in t and not any(w in t for w in _PP_INFRACTIONS):
        return False
    return True

def _detect_offsets(pens, away, home):
    """
    Group-based 1:1 coincidental offsetting.
    Validated: 145 cancelled pairs across 160 games, 0 false positives.
    Mutates pens in-place via is_coincidental flag.
    """
    COINCIDE = 8
    processed = set()
    for i, a in enumerate(pens):
        if i in processed: continue
        grp = [i]
        for j in range(len(pens)):
            if j == i or j in processed: continue
            if abs(pens[j]["elapsed"] - a["elapsed"]) <= COINCIDE:
                grp.append(j)
        for idx in grp: processed.add(idx)
        away_pp = [k for k in grp if pens[k]["team"] == away and pens[k]["creates"]]
        home_pp = [k for k in grp if pens[k]["team"] == home and pens[k]["creates"]]
        pairs   = min(len(away_pp), len(home_pp))
        for k in grp:             pens[k]["is_coincidental"] = False
        for k in away_pp[:pairs]: pens[k]["is_coincidental"] = True
        for k in home_pp[:pairs]: pens[k]["is_coincidental"] = True

def build_pp_windows_from_espn(espn_json, away_abbr, home_abbr):
    """
    Build PP windows directly from ESPN penalty plays.

    Every window has a PP Cause by construction (the penalty play itself).
    0 phantoms. 0 tagging failures. 100% PP Cause across 843 windows / 160 games.

    Returns list of dicts:
      ws, we:      int — window start/end elapsed seconds
      sit:         str — '5v4 PP', '4v5 PP', '5v3 PP', '3v5 PP', '4v3 PP', '3v4 PP'
      short:       str — 'away' or 'home' (penalised team)
      source_seq:  int — ESPN sequenceNumber of the cause penalty play
    """
    raw_plays = espn_json.get("plays") or []
    bm        = _build_bm(espn_json)

    # ── Collect all ESPN penalty plays ───────────────────────────────
    pens = []
    for p in raw_plays:
        type_obj = p.get("type", {}) if isinstance(p.get("type"), dict) else {}
        tid      = str(type_obj.get("id", ""))
        if not is_espn_penalty(tid): continue
        po   = p.get("period", {}) if isinstance(p.get("period"), dict) else {}
        pnum = po.get("number", 1)
        co   = p.get("clock", {}) if isinstance(p.get("clock"), dict) else {}
        elapsed = espn_clock_to_seconds(co.get("displayValue", "0:00"), pnum)
        team    = _resolve_team(p, bm)
        # Normalise: bm fallback returns "away"/"home" → convert to actual abbr
        if   team == "away": team = away_abbr
        elif team == "home": team = home_abbr
        text    = (p.get("text") or "")
        seq     = int(p.get("sequenceNumber", 0))

        # Duration: type.penaltyMinutes (primary) → text fallback
        pm = type_obj.get("penaltyMinutes") if isinstance(type_obj, dict) else None
        try:   mins = int(pm) if pm is not None else None
        except: mins = None
        if mins is None:
            txt = text.lower()
            if "double minor" in txt:                            mins = 4
            elif any(w in txt for w in ("major","match","fight")): mins = 5
            elif "misconduct" in txt:                            mins = 10
            else:                                                mins = 2
        if mins >= 10: continue

        pens.append({
            "elapsed": elapsed, "we": elapsed + mins * 60,
            "team": team, "text": text, "creates": _creates_pp(text),
            "is_coincidental": False,
            "is_dm": (mins == 4), "is_major": (mins == 5),
            "seq": seq, "pnum": pnum,
        })

    _detect_offsets(pens, away_abbr, home_abbr)

    # ── Collect PP goals for minor truncation ────────────────────────
    # Only goals by the non-penalised team truncate the window.
    pp_goals = []
    for p in raw_plays:
        to   = p.get("type", {}) if isinstance(p.get("type"), dict) else {}
        tt   = (to.get("text", "") or "").lower()
        txt  = (p.get("text") or "").lower()
        if "goal" not in tt or ("power play" not in txt and "power-play" not in txt):
            continue
        po   = p.get("period", {}) if isinstance(p.get("period"), dict) else {}
        pnum = po.get("number", 1)
        co   = p.get("clock", {}) if isinstance(p.get("clock"), dict) else {}
        gel    = espn_clock_to_seconds(co.get("displayValue", "0:00"), pnum)
        g_team = _resolve_team(p, bm)
        if   g_team == "away": g_team = away_abbr
        elif g_team == "home": g_team = home_abbr
        pp_goals.append({"el": gel, "team": g_team})

    # ── Build windows ────────────────────────────────────────────────
    windows = []
    for pen in pens:
        if pen["is_coincidental"] or not pen["creates"]: continue
        ws   = pen["elapsed"]; team = pen["team"]; pnum = pen["pnum"]

        # Direction: penalised team is short, opponent has advantage.
        if   team == away_abbr: short = "away"; base = "4v5 PP"
        elif team == home_abbr: short = "home"; base = "5v4 PP"
        else:                   short = "";     base = "PP"

        # 4v3 OT: period ≥ 4 (modern NHL 3v3 OT, one penalty → 4v3)
        if pnum >= 4:
            base = base.replace("4v5", "3v4").replace("5v4", "4v3")

        def _first_ppg(ws, we, short_team):
            return next(
                (g["el"] for g in pp_goals
                 if ws < g["el"] <= we and g["team"] != short_team),
                None
            )

        if pen["is_major"]:
            windows.append({"ws": ws, "we": pen["we"], "sit": base,
                            "short": short, "source_seq": pen["seq"]})
        elif pen["is_dm"]:
            mid = ws + 120
            g   = _first_ppg(ws, mid, team)
            if g:
                windows.append({"ws": ws,  "we": g + 1,    "sit": base,
                                "short": short, "source_seq": pen["seq"]})
                windows.append({"ws": mid, "we": pen["we"], "sit": base,
                                "short": short, "source_seq": pen["seq"]})
            else:
                windows.append({"ws": ws, "we": pen["we"], "sit": base,
                                "short": short, "source_seq": pen["seq"]})
        else:
            g   = _first_ppg(ws, pen["we"], team)
            end = (g + 1) if g else pen["we"]
            windows.append({"ws": ws, "we": end, "sit": base,
                            "short": short, "source_seq": pen["seq"]})

    return windows


def build_en_windows_from_nhl(nhl_plays, away_abbr, home_abbr):
    """
    EN windows from NHL situationCode digit scan (sit[1]==6 or sit[2]==6).
    Threshold ≥ 20s eliminates delayed-penalty goalie-pull noise (tops at 4-5s).

    Validated across 408 games (166 playoff + 242 regular season):
      Noise max: 4-5s  |  Real EN min: 20s  |  False positives at ≥20s: 0
      3v3 OT: 0 real EN windows (teams never pull in 3v3). Shootout: 0 windows.

    Returns list of {ws, we, dur, pulled}.
    """
    EN_MIN = 20
    out = []; in_en = False; ws = None; pulled = None; ws_p = None

    for p in sorted(nhl_plays, key=lambda x: (x["elapsed"], x.get("sort_order", 0))):
        pnum = p["period"]; el = p["elapsed"]
        sit  = p.get("sit_code", "") or ""; tkey = p.get("type_key", "")

        if tkey in ("period-start","period-end","game-end","shootout-complete"):
            if in_en and ws is not None:
                dur = el - ws
                if dur >= EN_MIN: out.append({"ws":ws,"we":el,"dur":dur,"pulled":pulled})
            in_en = False; ws = None; pulled = None; ws_p = None
            continue
        if len(sit) < 4: continue
        try: a = int(sit[1]); h = int(sit[2])
        except: continue
        is6 = (a == 6 or h == 6)

        if is6 and not in_en:
            in_en = True; ws = el; ws_p = pnum
            pulled = away_abbr if a == 6 else home_abbr
        elif is6 and in_en:
            if pnum != ws_p:
                dur = el - ws
                if dur >= EN_MIN: out.append({"ws":ws,"we":el,"dur":dur,"pulled":pulled})
                in_en = False; ws = None; pulled = None; ws_p = None
        elif not is6 and in_en:
            dur = el - ws
            if dur >= EN_MIN: out.append({"ws":ws,"we":el,"dur":dur,"pulled":pulled})
            in_en = False; ws = None; pulled = None; ws_p = None

    return out


def find_espn_situation(elapsed, pp_windows):
    """
    PP situation for an elapsed time from ESPN-first windows.
    Automatically handles 5v3 (two same-short-team overlapping windows).
    Returns e.g. '5v4 PP', '5v3 PP', '4v3 PP', '' (even strength).
    """
    matching = [w for w in pp_windows if w["ws"] <= elapsed <= w["we"]]
    if not matching: return ""
    if len(matching) == 1: return matching[0]["sit"]
    # Multiple overlapping windows
    ac = sum(1 for w in matching if w["short"] == "away")
    hc = sum(1 for w in matching if w["short"] == "home")
    if ac >= 2 and hc == 0: return "3v5 PP"   # away has 2 in box
    if hc >= 2 and ac == 0: return "5v3 PP"   # home has 2 in box
    return matching[0]["sit"]


# =========================
# ESPN SCOREBOARD
# =========================
@st.cache_data(ttl=30, show_spinner=False)
def fetch_scoreboard(date_str):
    try:
        data = requests.get(
            ESPN_SCOREBOARD,
            params={"dates": date_str.replace("-", ""), "limit": 25},
            timeout=10,
        ).json()
    except Exception as e:
        st.error(f"ESPN error: {e}")
        return []

    games = []
    for event in data.get("events", []):
        comp       = event.get("competitions", [{}])[0]
        status     = comp.get("status", {})
        state_type = status.get("type", {})
        state      = state_type.get("state", "pre")
        raw_name   = state_type.get("name", "")
        display_status = "Scheduled" if raw_name == "STATUS_SCHEDULED" else state_type.get("shortDetail", "")

        competitors = comp.get("competitors", [])
        try:
            away = next(c for c in competitors if c.get("homeAway") == "away")
            home = next(c for c in competitors if c.get("homeAway") == "home")
        except StopIteration:
            continue

        start_dt = to_et(event.get("date", ""))
        is_final = state == "post"
        is_live  = state == "in"

        games.append({
            "event_id":    event.get("id", ""),
            "state":       state,
            "state_name":  display_status,
            "is_live":     is_live,
            "is_final":    is_final,
            "is_ot":       status.get("period", 0) > 3,
            "away_abbr":   away.get("team", {}).get("abbreviation", "?"),
            "home_abbr":   home.get("team", {}).get("abbreviation", "?"),
            "away_logo":   away.get("team", {}).get("logo", ""),
            "home_logo":   home.get("team", {}).get("logo", ""),
            "away_score":  int(away.get("score", 0) or 0),
            "home_score":  int(home.get("score", 0) or 0),
            "has_score":   is_live or is_final,
            "time_str":    fmt_game_time(start_dt),
            "date_str":    date_str,
        })
    return sorted(games, key=lambda x: x["time_str"])

# =========================
# HYBRID PLAY PARSER
# =========================
def get_parsed_plays(event_id, nhl_game_id, away_abbr="", home_abbr=""):
    # Return cached plays if available
    if st.session_state.cached_event_id == event_id and st.session_state.cached_plays:
        return st.session_state.cached_plays

    # ── Fetch ESPN ────────────────────────────────────────────────────
    try:
        resp = requests.get(ESPN_SUMMARY, params={"event": event_id}, timeout=15)
        resp.raise_for_status()
        espn_json = resp.json()
        raw_plays = espn_json.get("plays", [])
    except Exception as e:
        st.error(f"ESPN error: {e}")
        return []

    # ── Fetch NHL (delayed-penalty events + EN sit codes) ─────────────
    bucket   = st.session_state.get("force_bucket") or int(time.time() // 30)
    nhl_data = (fetch_nhl_plays(nhl_game_id, cache_bucket=bucket)
                if nhl_game_id
                else {"plays": [], "delayed_events": [], "teams": {}})

    st.session_state.last_refresh = datetime.now(ET)

    # ── Build ESPN-first PP windows ───────────────────────────────────
    # Validated: 843 windows · 160 games · 100% PP Cause · 0 phantoms
    pp_windows = build_pp_windows_from_espn(espn_json, away_abbr, home_abbr)

    # ── Build EN windows from NHL sit codes (≥20s) ────────────────────
    # Validated: 0 false positives across 408 games, all game modes
    en_windows = build_en_windows_from_nhl(nhl_data["plays"], away_abbr, home_abbr)

    # ── Index PP windows by source play sequenceNumber ────────────────
    # PP Cause is by construction — penalty play that built the window
    pp_cause_seqs = {w["source_seq"] for w in pp_windows}
    seq_to_win    = {w["source_seq"]: w for w in pp_windows}

    # ── Parse ESPN plays ──────────────────────────────────────────────
    plays       = []
    last_pp_sit = None   # carry-forward for SH plays outside windows (2.4% gap)

    for p in sorted(raw_plays, key=lambda x: int(x.get("sequenceNumber", 0))):
        po   = p.get("period", {}) if isinstance(p.get("period"), dict) else {}
        pnum = po.get("number", 1)
        ptype= po.get("type", "")
        co   = p.get("clock", {}) if isinstance(p.get("clock"), dict) else {}
        clk  = co.get("displayValue", "") if isinstance(co, dict) else str(co)
        to   = p.get("type", {}) if isinstance(p.get("type"), dict) else {}
        type_text = to.get("text", "") if isinstance(to, dict) else str(to)
        type_id   = to.get("id",   "") if isinstance(to, dict) else ""
        text     = p.get("text", "")
        wall_raw = p.get("wallclock", "")
        seq      = int(p.get("sequenceNumber", 0))
        elapsed  = espn_clock_to_seconds(clk, pnum)

        # ── Strength: window overlay → EN → ESPN carry-forward ────────
        so      = p.get("strength", {}) if isinstance(p.get("strength"), dict) else {}
        str_id  = str(so.get("id", "")) if isinstance(so, dict) else ""
        str_txt = (so.get("text", "") or "") if isinstance(so, dict) else ""

        pp_sit = find_espn_situation(elapsed, pp_windows)

        if pp_sit:
            situation   = pp_sit
            last_pp_sit = pp_sit
        elif str_txt in ("Power Play", "Shorthanded"):
            # ESPN confirms PP/SH but play falls just outside window boundary
            situation = last_pp_sit if last_pp_sit else str_txt
        elif str_id == "903":
            # ESPN EN goal tag — always authoritative
            situation   = "EN"
            last_pp_sit = None
        elif any(w["ws"] <= elapsed <= w["we"] for w in en_windows):
            # Inside validated NHL EN window (≥20s, 0 false positives)
            situation   = "EN"
            last_pp_sit = None
        else:
            last_pp_sit = None
            situation   = ""

        # ── PP Cause: by construction from window source_seq ─────────
        is_pp_cause = (seq in pp_cause_seqs)
        if is_pp_cause:
            win      = seq_to_win[seq]
            pp_arrow = f"5v5 → {win['sit']}"
        else:
            pp_arrow = ""

        plays.append({
            "seq":          seq,
            "period_num":   pnum,
            "period_type":  ptype,
            "period_label": period_label(pnum, ptype),
            "clock":        clk,
            "elapsed":      elapsed,
            "type_text":    type_text,
            "type_id":      type_id,
            "text":         text,
            "situation":    situation,
            "wall_raw":     wall_raw,
            "wall_et":      fmt_et_full(wall_raw),
            "wall_dt":      to_et(wall_raw),
            "away_score":   p.get("awayScore", ""),
            "home_score":   p.get("homeScore", ""),
            "emoji":        get_play_emoji(type_text),
            "is_pp_cause":  is_pp_cause,
            "pp_arrow":     pp_arrow,
            "is_delayed":   False,
            "is_carryover": False,
        })

    # ── Inject Delayed Penalty cards (from NHL delayed-penalty events) ─
    away_id = str(nhl_data.get("teams", {}).get("away_id", "") or "")
    home_id = str(nhl_data.get("teams", {}).get("home_id", "") or "")

    for dp in nhl_data.get("delayed_events", []):
        dp_el  = dp["elapsed"]; dp_pnum = dp["period"]
        oid    = str(dp.get("owner_id", "") or "")
        if   oid == away_id: dp_txt = f"Referee arm raised — {away_abbr} drawing delayed penalty"
        elif oid == home_id: dp_txt = f"Referee arm raised — {home_abbr} drawing delayed penalty"
        else:                dp_txt = "Referee arm raised — Delayed Penalty in progress"

        _fwd  = [p for p in plays if p["elapsed"] >= dp_el]
        _near = min(_fwd, key=lambda p: p["elapsed"]) if _fwd else \
                (min(plays, key=lambda p: abs(p["elapsed"] - dp_el)) if plays else None)

        plays.append({
            "seq":          -2,
            "period_num":   dp_pnum,
            "period_type":  "REG",
            "period_label": f"P{dp_pnum}",
            "clock":        (_near.get("clock", "")      if _near else ""),
            "elapsed":      dp_el,
            "type_text":    "Delayed Penalty",
            "type_id":      "",
            "text":         dp_txt,
            "situation":    "Delayed Penalty",
            "wall_raw":     "",
            "wall_et":      (_near.get("wall_et", "")    if _near else ""),
            "wall_dt":      None,
            "away_score":   (_near.get("away_score", "") if _near else ""),
            "home_score":   (_near.get("home_score", "") if _near else ""),
            "emoji":        "🖐️",
            "is_pp_cause":  False,
            "pp_arrow":     "",
            "is_delayed":   True,
            "is_carryover": False,
        })

    # ── Inject Carry-over cards (PP window spans period boundary) ─────
    # Detect period-start plays that fall inside a PP window.
    period_start_els = {
        p["elapsed"] for p in plays
        if (p.get("type_text") or "").lower() in ("period start", "period-start")
        or p.get("type_id") in ("12", "")  # ESPN period-start type
    }
    # Fallback: infer period starts from elapsed arithmetic
    for pn in range(1, 7):
        period_start_els.add(pn * 1200)

    for win in pp_windows:
        for ps_el in period_start_els:
            if win["ws"] < ps_el < win["we"]:
                _fwd  = [p for p in plays if p["elapsed"] > ps_el]
                _near = min(_fwd, key=lambda p: p["elapsed"]) if _fwd else None
                co_pnum = ps_el // 1200 + 1
                plays.append({
                    "seq":          -3,
                    "period_num":   co_pnum,
                    "period_type":  "REG",
                    "period_label": f"P{co_pnum}",
                    "clock":        "0:00",
                    "elapsed":      ps_el,
                    "type_text":    "Carry-over penalty",
                    "type_id":      "",
                    "text":         "Power play continues from previous period penalty",
                    "situation":    win["sit"],
                    "wall_raw":     "",
                    "wall_et":      "",
                    "wall_dt":      None,
                    "away_score":   (_near.get("away_score", "") if _near else ""),
                    "home_score":   (_near.get("home_score", "") if _near else ""),
                    "emoji":        "🔄",
                    "is_pp_cause":  False,
                    "pp_arrow":     "",
                    "is_delayed":   False,
                    "is_carryover": True,
                })

    plays.sort(key=lambda p: (p.get("elapsed", 0), p.get("seq", 0)))

    st.session_state.cached_plays    = plays
    st.session_state.cached_event_id = event_id
    return plays


# =========================
# CSS
# =========================
st.markdown("""
<style>
div[data-testid="stVerticalBlockBorderWrapper"] { min-height: 150px; }
.sched-team-row { display: flex; align-items: center; gap: 12px; margin-bottom: 6px; }
.sched-team-name { font-size: 22px; font-weight: 800; color: #ffffff; }
.sched-score { font-size: 22px; font-weight: 800; color: #888888; margin-left: auto; }
.sched-meta { font-size: 13px; color: #999999; border-top: 1px solid rgba(255,255,255,0.1);
    padding-top: 8px; margin-top: 8px; display: flex; align-items: center; }
.sched-extra { background: #e67e22; color: #fff; font-size: 11px; padding: 2px 6px;
    border-radius: 4px; margin-left: 8px; font-weight: bold; }
</style>
""", unsafe_allow_html=True)

# ======================================================
# GAME FEED VIEW
# ======================================================
if st.session_state.view == "game":

    plays = get_parsed_plays(
        st.session_state.event_id,
        st.session_state.nhl_game_id or "",
        st.session_state.away,
        st.session_state.home,
    )

    nav_col1, nav_col2, nav_col3, nav_col4, _ = st.columns([1.3, 0.9, 1.1, 1.8, 4.9])
    with nav_col1:
        if st.button("⬅ Back to Schedule", use_container_width=True):
            st.session_state.view            = "schedule"
            st.session_state.filters_applied = False
            st.session_state.filtered_plays  = None
            st.rerun()
    with nav_col2:
        if st.button("🔄 Refresh", use_container_width=True):
            # Force a new cache bucket so fetch_nhl_plays gets fresh data
            # regardless of any shared function cache on Streamlit Cloud
            st.session_state.force_bucket    = int(time.time() // 30) + 1
            st.session_state.cached_plays    = None
            st.session_state.cached_event_id = None
            st.rerun()
    with nav_col3:
        sort_label = "↓ Oldest first" if not st.session_state.sort_newest_first else "↑ Newest first"
        sort_type  = "secondary" if not st.session_state.sort_newest_first else "primary"
        if st.button(sort_label, use_container_width=True, type=sort_type):
            st.session_state.sort_newest_first = not st.session_state.sort_newest_first
            st.rerun()
    with nav_col4:
        refresh_time = st.session_state.last_refresh.strftime("%H:%M:%S ET")
        st.markdown(
            f'<div style="background-color:#2e7d32;color:white;padding:8px 16px;'
            f'border-radius:4px;font-size:14px;font-weight:bold;text-align:center;">'
            f'Last refresh {refresh_time}</div>',
            unsafe_allow_html=True,
        )

    st.markdown("<br>", unsafe_allow_html=True)

    # Read live score from cached scoreboard (TTL=30s) — falls back to
    # session state if the event is not found or the fetch fails.
    _live_away = st.session_state.away_score
    _live_home = st.session_state.home_score
    try:
        _board = fetch_scoreboard(st.session_state.sched_date.strftime("%Y-%m-%d"))
        for _g in _board:
            if str(_g["event_id"]) == str(st.session_state.event_id):
                _live_away = _g["away_score"]
                _live_home = _g["home_score"]
                break
    except Exception:
        pass

    head_c1, head_c2, head_c3 = st.columns([1, 6, 1])
    with head_c1:
        if st.session_state.away_logo:
            st.image(st.session_state.away_logo, width=80)
    with head_c2:
        st.markdown(f"""
            <div style="display:flex;align-items:center;justify-content:center;
                font-weight:800;font-size:clamp(20px,3vw,32px);gap:15px;text-align:center;">
                <span>{st.session_state.away}</span>
                <span style="color:#888;">{_live_away}</span>
                <span style="color:#444;">-</span>
                <span style="color:#888;">{_live_home}</span>
                <span>{st.session_state.home}</span>
            </div>
        """, unsafe_allow_html=True)
    with head_c3:
        if st.session_state.home_logo:
            st.image(st.session_state.home_logo, width=80)

    nhl_id = st.session_state.nhl_game_id
    st.caption(
        f"📡 ESPN-primary · NHL `{nhl_id}` (delayed-penalty + EN)" if nhl_id
        else "📡 ESPN only"
    )

    st.divider()

    # ── Filters ───────────────────────────────────────────────────────────
    raw_periods = list({p["period_label"] for p in plays})
    def p_key(l):
        if l.startswith("P"): return int(l[1:])
        if l == "OT": return 100
        if l.startswith("OT"): return 100 + int(l[2:])
        return 200
    all_periods = sorted(raw_periods, key=p_key)

    all_dts  = [p["wall_dt"] for p in plays if p["wall_dt"]]
    g_start  = min(all_dts) if all_dts else None
    g_end    = max(all_dts) if all_dts else None

    USE_PERIOD_FILTER = st.checkbox("🏒 Filter by Period", value=False, key="cb_period")
    selected_periods  = st.multiselect("Select Periods", options=all_periods) if USE_PERIOD_FILTER else []

    USE_TIME_FILTER = st.checkbox("🕐 Filter by Actual Time (ET)", value=False, key="cb_time")
    START_DT = END_DT = None
    if USE_TIME_FILTER:
        def_sd = g_start.date() if g_start else ddate.today()
        def_ed = g_end.date()   if g_end   else ddate.today()
        def_st = g_start.time() if g_start else dtime(19, 0)
        def_et = g_end.time()   if g_end   else dtime(23, 59)
        st.markdown("**Start date/time (ET)**")
        sc1, sc2 = st.columns(2)
        with sc1: start_date_input = st.date_input("Start date", value=def_sd, key="tf_start_date")
        with sc2: start_time_input = st.time_input("Start time", value=def_st, step=60, key="tf_start_time")
        st.markdown("**End date/time (ET)**")
        ec1, ec2 = st.columns(2)
        with ec1: end_date_input = st.date_input("End date", value=def_ed, key="tf_end_date")
        with ec2: end_time_input = st.time_input("End time", value=def_et, step=60, key="tf_et")
        START_DT = datetime.combine(start_date_input, start_time_input).replace(tzinfo=ET)
        END_DT   = datetime.combine(end_date_input,   end_time_input).replace(tzinfo=ET)

    USE_GOAL_FILTER = st.checkbox("🚨 Goals Only",       value=False, key="cb_goals")
    USE_PP_FILTER   = st.checkbox("⚡ Power Plays Only", value=False, key="cb_pp")
    USE_GP_FILTER   = st.checkbox("🥅 Empty Nets Only",  value=False, key="cb_en")

    btn_col1, btn_col2, _ = st.columns([1.5, 1.5, 7])

    with btn_col1:
        if st.button("🚀 Apply Filters", use_container_width=True):
            def passes(p):
                sit = p.get("situation", "")
                if USE_PERIOD_FILTER and selected_periods and p["period_label"] not in selected_periods:
                    return False
                if USE_TIME_FILTER:
                    if not p["wall_dt"] or START_DT is None or END_DT is None: return False
                    if not (START_DT <= p["wall_dt"] <= END_DT): return False
                if USE_GOAL_FILTER and p["type_text"] != "Goal": return False
                if USE_PP_FILTER and " PP" not in sit and not p.get("is_pp_cause", False) and not p.get("is_delayed", False) and not p.get("is_carryover", False): return False
                if USE_GP_FILTER and "EN" not in sit: return False
                return True
            st.session_state.filtered_plays  = [p for p in plays if passes(p)]
            st.session_state.filters_applied = True
            # Snapshot which filters were active at Apply time —
            # info labels only read from this, not live checkbox state
            st.session_state.active_filter_snapshot = {
                "period":     USE_PERIOD_FILTER,
                "periods":    list(selected_periods),
                "time":       USE_TIME_FILTER,
                "start_dt":   START_DT,
                "end_dt":     END_DT,
                "goals":      USE_GOAL_FILTER,
                "pp":         USE_PP_FILTER,
                "en":         USE_GP_FILTER,
            }
            st.rerun()

    with btn_col2:
        def reset_filters():
            st.session_state.filters_applied        = False
            st.session_state.filtered_plays         = None
            st.session_state.active_filter_snapshot = {}
            st.session_state.cb_period = False
            st.session_state.cb_time   = False
            st.session_state.cb_goals  = False
            st.session_state.cb_pp     = False
            st.session_state.cb_en     = False
        # Disabled when no filters are active (fix #3)
        filters_currently_applied = st.session_state.get("filters_applied", False)
        st.button(
            "🗑️ Remove Filters",
            use_container_width=True,
            on_click=reset_filters,
            disabled=not filters_currently_applied,
        )

    filters_applied = st.session_state.get("filters_applied")
    display_list    = st.session_state.filtered_plays if filters_applied else plays
    # Fix 2: reverse display order without mutating the stored list
    if st.session_state.sort_newest_first:
        display_list = display_list[::-1]
    total   = len(plays)
    showing = len(display_list)

    # Info labels read from snapshot (fix #4) — only shows filters
    # that were active when Apply was last pressed, not current checkboxes
    if filters_applied:
        snap = st.session_state.get("active_filter_snapshot", {})
        if showing == 0:
            st.warning("⚠️ No results found — please check the filters applied.")
            st.stop()
        if snap.get("period"):
            labels = snap["periods"] if snap["periods"] else ["none selected"]
            st.info(f"🏒 **Period filter:** {', '.join(labels)} — showing **{showing}** of **{total}** plays")
        if snap.get("time") and snap.get("start_dt") and snap.get("end_dt"):
            st.info(f"🕐 **Time filter:** {snap['start_dt'].strftime('%Y-%m-%d %H:%M')} → {snap['end_dt'].strftime('%Y-%m-%d %H:%M')} ET — showing **{showing}** of **{total}** plays")
        if snap.get("goals"):
            n_goals = sum(1 for p in plays if p["type_text"] == "Goal")
            st.info(f"🚨 **Goals Only:** {n_goals} goal(s) in game — showing **{showing}** of **{total}** plays")
        if snap.get("pp"):
            st.info(f"⚡ **Power Plays Only:** showing **{showing}** of **{total}** plays")
        if snap.get("en"):
            st.info(f"🥅 **Empty Nets Only:** showing **{showing}** of **{total}** plays")

    for p in display_list:
        emoji        = "🚨" if p.get("type_text") == "Goal" else p.get("emoji", "🏒")
        sit          = p.get("situation", "")
        is_pp_cause  = p.get("is_pp_cause", False)

        if is_pp_cause:
            strength_display = p.get("pp_arrow") or sit
            wall_et = p.get("wall_et", "")
            time_row = (f'<p style="margin:12px 0 0 0;font-size:1rem">🕐 <b>Time (ET):</b> <code>{wall_et}</code></p>'
                        if wall_et and wall_et != "N/A" else "")
            strength_row = (f'<p style="margin:12px 0 0 0;font-size:1rem">⚖️ <b>Strength:</b> <code>{strength_display}</code></p>'
                            if strength_display else "")
            st.markdown(f"""
<div style="border-left:3px solid #BA7517;padding-left:12px;margin:20px 0 0 0;border-radius:0">
  <div style="display:flex;align-items:center;gap:10px;margin:0 0 12px 0">
    <span style="font-size:1.5rem;font-weight:600;line-height:1.3">{emoji} {p.get('period_label')} | ⏱️ {p.get('clock')}</span>
    <span style="background:#FAEEDA;color:#854F0B;font-size:12px;font-weight:500;padding:2px 8px;border-radius:4px;white-space:nowrap">PP Cause</span>
  </div>
  <p style="margin:12px 0 0 0;font-size:1rem">📊 <b>Score:</b> {p.get('away_score')} - {p.get('home_score')}</p>
  <p style="margin:12px 0 0 0;font-size:1rem">🎯 <b>Event:</b> {p.get('type_text')}</p>
  {strength_row}
  <p style="margin:12px 0 0 0;font-size:1rem">📋 <b>Play:</b> {p.get('text')}</p>
  {time_row}
</div>
""", unsafe_allow_html=True)
        elif p.get("is_delayed"):
            # Delayed penalty card — blue border + badge
            # Timestamp is approximate (nearest ESPN play); shown with ~ prefix.
            wall_et = p.get("wall_et", "")
            time_row = (
                f'<p style="margin:12px 0 0 0;font-size:1rem">🕐 <b>Time (ET):</b>'
                f' <code>approx {wall_et}</code></p>'
                if wall_et and wall_et != "N/A" else ""
            )
            st.markdown(f"""
<div style="border-left:3px solid #185FA5;padding-left:12px;margin:20px 0 0 0;border-radius:0">
  <div style="display:flex;align-items:center;gap:10px;margin:0 0 12px 0">
    <span style="font-size:1.5rem;font-weight:600;line-height:1.3">{emoji} {p.get('period_label')} | ⏱️ {p.get('clock')}</span>
    <span style="background:#E6F1FB;color:#185FA5;font-size:12px;font-weight:500;padding:2px 8px;border-radius:4px;white-space:nowrap">Delayed Penalty</span>
  </div>
  <p style="margin:12px 0 0 0;font-size:1rem">📊 <b>Score:</b> {p.get('away_score')} - {p.get('home_score')}</p>
  <p style="margin:12px 0 0 0;font-size:1rem">🎯 <b>Event:</b> {p.get('type_text')}</p>
  <p style="margin:12px 0 0 0;font-size:1rem">📋 <b>Play:</b> {p.get('text')}</p>
  {time_row}
</div>
""", unsafe_allow_html=True)
        elif p.get("is_carryover"):
            # Carry-over penalty card — green border + badge
            # Appears when a penalty from a previous period or earlier in the
            # same period is still actively serving at the start of this window.
            wall_et = p.get("wall_et", "")
            time_row = (
                f'<p style="margin:12px 0 0 0;font-size:1rem">🕐 <b>Time (ET):</b>'
                f' <code>approx {wall_et}</code></p>'
                if wall_et and wall_et != "N/A" else ""
            )
            st.markdown(f"""
<div style="border-left:3px solid #1A7A4A;padding-left:12px;margin:20px 0 0 0;border-radius:0">
  <div style="display:flex;align-items:center;gap:10px;margin:0 0 12px 0">
    <span style="font-size:1.5rem;font-weight:600;line-height:1.3">🔄 {p.get('period_label')} | ⏱️ {p.get('clock')}</span>
    <span style="background:#E6F4EC;color:#1A7A4A;font-size:12px;font-weight:500;padding:2px 8px;border-radius:4px;white-space:nowrap">Carry-over</span>
  </div>
  <p style="margin:12px 0 0 0;font-size:1rem">📊 <b>Score:</b> {p.get('away_score')} - {p.get('home_score')}</p>
  <p style="margin:12px 0 0 0;font-size:1rem">⚖️ <b>Strength:</b> <code>{p.get('situation')}</code></p>
  <p style="margin:12px 0 0 0;font-size:1rem">📋 <b>Play:</b> {p.get('text')}</p>
  {time_row}
</div>
""", unsafe_allow_html=True)
        else:
            # Standard render
            st.subheader(f"{emoji} {p.get('period_label')} | ⏱️ {p.get('clock')}")
            st.markdown(f"📊 **Score:** {p.get('away_score')} - {p.get('home_score')}")
            st.markdown(f"🎯 **Event:** {p.get('type_text')}")
            if " PP" in sit or sit == "EN":
                st.markdown(f"⚖️ **Strength:** `{sit}`")
            st.markdown(f"📋 **Play:** {p.get('text')}")
            if p.get("wall_et") and p.get("wall_et") != "N/A":
                st.markdown(f"🕐 **Time (ET):** `{p['wall_et']}`")

        st.divider()

# ======================================================
# SCHEDULE VIEW
# ======================================================
else:
    def handle_date_change():
        st.session_state.sched_date = st.session_state.calendar_widget

    date = st.date_input(
        "Select date",
        value=st.session_state.sched_date,
        key="calendar_widget",
        on_change=handle_date_change,
    )
    formatted_date = date.strftime("%Y-%m-%d")
    games = fetch_scoreboard(formatted_date)

    if not games:
        st.info(f"No games scheduled for {formatted_date}.")
    else:
        cols = st.columns(2)
        for i, g in enumerate(games):
            has_started = g["has_score"]
            ot_badge    = '<span class="sched-extra">OT</span>' if g["is_ot"] else ""
            card_html   = f"""
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
                    btn_label = (
                        f"▶ Open {g['away_abbr']} @ {g['home_abbr']}"
                        if has_started else "⏳ Not Started"
                    )
                    if st.button(
                        btn_label,
                        key=f"btn_{g['event_id']}",
                        use_container_width=True,
                        disabled=not has_started,
                        help="" if has_started else "Data available once game starts.",
                    ):
                        nhl_id = find_nhl_game_id(
                            formatted_date,
                            g["away_abbr"],
                            g["home_abbr"],
                        )
                        st.session_state.update({
                            "view":            "game",
                            "event_id":        g["event_id"],
                            "nhl_game_id":     nhl_id,
                            "away":            g["away_abbr"],
                            "home":            g["home_abbr"],
                            "away_logo":       g["away_logo"],
                            "home_logo":       g["home_logo"],
                            "away_score":      g["away_score"],
                            "home_score":      g["home_score"],
                            "game_state":      g["state"],
                            "filters_applied": False,
                            "filtered_plays":  None,
                            "cached_plays":    None,
                            "cached_event_id": None,
                        })
                        st.rerun()
