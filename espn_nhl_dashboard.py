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
# Penalty types that do NOT create a power play:
NO_PP_PENALTY_TYPES = {
    "misconduct",       # 10-min, player replaced
    "game-misconduct",  # rest of game, player replaced
    "fighting",         # offsetting, 5v5 continues unless one side has more
}

@st.cache_data(show_spinner=False)
def fetch_nhl_plays(nhl_game_id, cache_bucket: int = 0):
    """
    Fetch NHL play-by-play and parse plays + penalty details.
    Returns dict with:
      - plays: list of plays with sit_code/situation
      - penalties: list of penalty details (duration, type, team)
      - teams: {'away_id': ..., 'home_id': ...}
    """
    if not nhl_game_id:
        return {"plays": [], "penalties": [], "teams": {}}
    try:
        resp = requests.get(f"{NHL_PBP}/{nhl_game_id}/play-by-play", timeout=10)
        resp.raise_for_status()
        data = resp.json()
    except Exception:
        return {"plays": [], "penalties": [], "teams": {}}

    teams = {
        "away_id": data.get("awayTeam", {}).get("id"),
        "home_id": data.get("homeTeam", {}).get("id"),
        "away_abbr": (data.get("awayTeam", {}).get("abbrev") or "").upper(),
        "home_abbr": (data.get("homeTeam", {}).get("abbrev") or "").upper(),
    }

    plays = []
    penalties = []

    # ── Option A/D field capture: rosterSpots for diagnostic verification ─
    # Build playerId→teamId map from rosterSpots (if present in response).
    # NOT yet used for direction logic — captured here so the diagnostic
    # expander can show whether the field is populated and what it contains.
    raw_roster = data.get("rosterSpots", [])
    roster_map = {}  # playerId → teamId — populated only if field exists
    for spot in raw_roster:
        pid = spot.get("playerId")
        tid = spot.get("teamId")
        if pid and tid:
            roster_map[pid] = tid

    for p in data.get("plays", []):
        pd       = p.get("periodDescriptor", {})
        pnum     = pd.get("number", 1)
        ptype    = pd.get("periodType", "REG")
        tip      = p.get("timeInPeriod", "20:00")
        sit      = p.get("situationCode") or ""
        type_key = p.get("typeDescKey", "")
        details  = p.get("details") or {}

        elapsed = nhl_clock_to_seconds(tip, pnum)

        plays.append({
            "period":     pnum,
            "period_type":ptype,
            "elapsed":    elapsed,
            "time_in":    tip,
            "type_key":   type_key,
            "sit_code":   sit,
            "situation":  parse_nhl_situation(sit),
            "sort_order": p.get("sortOrder", 0),
        })

        # Extract penalty details — NHL API gives us the duration directly
        if type_key in ("penalty", "delayed-penalty"):
            desc_key       = (details.get("descKey") or "").lower()
            duration_min   = details.get("duration")   # already in minutes
            committed_id   = details.get("committedByPlayerId")
            drawn_id       = details.get("drawnByPlayerId")    # Option D field
            event_team_id  = details.get("eventOwnerTeamId")  # team that took penalty

            # Determine penalty side — three-tier resolution:
            #   1. eventOwnerTeamId (primary — always preferred)
            #   2. Option A: committedByPlayerId → rosterSpots
            #      committedByPlayerId = player who DREW/benefited (NHL naming is
            #      inverted vs natural language). Their team = drawing team = pen_side.
            #   3. Option D: drawnByPlayerId → rosterSpots
            #      drawnByPlayerId = player who COMMITTED (also inverted naming).
            #      Their team = committing team → other team = drawing team.
            # Convention verified Game 4: A+D each matched pen_side on 8/8 penalties.
            pen_side = ""
            if event_team_id == teams["away_id"]:
                pen_side = "away"
            elif event_team_id == teams["home_id"]:
                pen_side = "home"

            # Option A: fallback via committedByPlayerId → rosterSpots
            if not pen_side and committed_id and roster_map:
                pid_team = roster_map.get(committed_id)
                if pid_team == teams["away_id"]:
                    pen_side = "away"
                elif pid_team == teams["home_id"]:
                    pen_side = "home"

            # Option D: final fallback via drawnByPlayerId → rosterSpots
            if not pen_side and drawn_id and roster_map:
                drawn_team = roster_map.get(drawn_id)
                if drawn_team == teams["away_id"]:
                    pen_side = "home"   # away committed → home drew → home benefited
                elif drawn_team == teams["home_id"]:
                    pen_side = "away"   # home committed → away drew → away benefited

            # ── Diagnostic capture — always run regardless of pen_side source ─
            # diag_source: which tier actually resolved pen_side
            # diag_a/diag_d: roster lookup result for each player ID computed
            #   independently so the expander shows whether A+D WOULD resolve
            #   direction even when the primary source already did.
            if event_team_id in (teams["away_id"], teams["home_id"]):
                diag_source = "primary"
            elif committed_id and roster_map and roster_map.get(committed_id):
                diag_source = "Option A"
            elif drawn_id and roster_map and roster_map.get(drawn_id):
                diag_source = "Option D"
            else:
                diag_source = "unresolved"

            diag_a = ""
            if committed_id and roster_map:
                t = roster_map.get(committed_id)
                if t == teams["away_id"]:   diag_a = "away"
                elif t == teams["home_id"]: diag_a = "home"

            diag_d = ""
            if drawn_id and roster_map:
                t = roster_map.get(drawn_id)
                if t == teams["away_id"]:   diag_d = "home"
                elif t == teams["home_id"]: diag_d = "away"

            penalties.append({
                "elapsed":        elapsed,
                "period":         pnum,
                "period_type":    ptype,
                "desc_key":       desc_key,
                "duration_min":   duration_min,
                "duration_sec":   (duration_min or 2) * 60,
                "pen_side":       pen_side,
                "is_no_pp":       desc_key in NO_PP_PENALTY_TYPES,
                "type_key":       type_key,
                "sort_order":     p.get("sortOrder", 0),
                # Diagnostic fields — not used in direction logic
                "committed_id":   committed_id,
                "drawn_id":       drawn_id,
                "diag_source":    diag_source,    # which tier resolved pen_side
                "diag_a":         diag_a,         # roster lookup via committedId
                "diag_d":         diag_d,         # roster lookup via drawnId
            })

    return {
        "plays":       plays,
        "penalties":   penalties,
        "teams":       teams,
        # Diagnostic fields for expander verification
        "roster_map":      roster_map,        # Option A: {playerId: teamId}
        "roster_spots_raw": len(raw_roster),  # count of rosterSpots entries
    }

# =========================
# COINCIDENTAL PENALTY DETECTION
# =========================
def detect_coincidental_penalties(penalties):
    """
    NHL rule: when both teams take penalties at the same stoppage with
    matching minor durations, they are 'coincidental' — no PP, just 4v4.

    Mark coincidental pairs as is_no_pp=True so they're excluded from
    PP window generation. Penalties within 2 seconds of each other on
    opposing teams with the same duration are considered coincidental.
    """
    for i, p in enumerate(penalties):
        if p["is_no_pp"]:
            continue
        for j, q in enumerate(penalties):
            if i == j or q["is_no_pp"]:
                continue
            if (p["pen_side"] != q["pen_side"]
                and p["pen_side"] and q["pen_side"]
                and abs(p["elapsed"] - q["elapsed"]) <= 2
                and p["duration_min"] == q["duration_min"]
                and p["duration_min"] in (2, 4)):  # minors / double minors coincide
                # Mark both as coincidental
                p["is_no_pp"]   = True
                p["coincident"] = True
                q["is_no_pp"]   = True
                q["coincident"] = True
    return penalties

# =========================
# ESPN PENALTY PARSER (FALLBACK)
# =========================
ESPN_PENALTY_DURATIONS = [
    ("double minor",    240),
    ("game misconduct", None),
    ("misconduct",      None),
    ("major",           300),
    ("match",           300),
    ("bench minor",     120),
    ("minor",           120),
]
ESPN_NO_PP_KEYWORDS = [
    "coincidental", "offsetting", "matching penalties",
    "double minor to each", "roughing to each",
]

def parse_espn_penalties(espn_plays, away_abbr, home_abbr):
    """
    Fallback penalty parser using ESPN play type.id for detection.
    Uses is_espn_penalty(type_id) — the same confirmed ID set used for
    is_pp_cause tagging — so any play detected as a penalty here is
    consistent with what gets tagged in the feed.

    Direction convention (matches NHL sit code XvY = away_sk v home_sk):
      away team committed penalty → away short → 4v5 PP (4 away, 5 home)
      home team committed penalty → home short → 5v4 PP (5 away, 4 home)

    Duration: extracted from play text where present, default 120s (minor).
    """
    import re

    penalties = []
    for p in espn_plays:
        type_obj  = p.get("type", {})
        type_id   = type_obj.get("id", "") if isinstance(type_obj, dict) else ""
        if not is_espn_penalty(type_id):
            continue

        text = (p.get("text") or "").lower()

        # Skip no-PP situations (coincidental, offsetting)
        if any(kw in text for kw in ESPN_NO_PP_KEYWORDS):
            continue

        # Determine duration from keyword match (longest first)
        duration = None
        is_dm = is_major = False
        for key, dur in ESPN_PENALTY_DURATIONS:
            if key in text:
                duration = dur
                is_dm    = (key == "double minor")
                is_major = key in ("major", "match")
                break

        # Fallback: extract numeric duration from "(X min)" patterns
        if duration is None:
            m = re.search(r"\((\d+)\s*min\)", text) or re.search(r"\((\d+):00\)", text)
            if m:
                mins = int(m.group(1))
                if mins == 4:
                    duration = 240
                    is_dm    = True
                elif mins == 5:
                    duration = 300
                    is_major = True
                elif mins == 10:
                    duration = 0      # misconduct — skip below
                else:
                    duration = 120    # default 2-min minor

        # Final default — assume minor if nothing else found
        if duration is None:
            duration = 120

        if duration == 0:
            continue

        period_obj = p.get("period", {})
        pnum       = period_obj.get("number", 1) if isinstance(period_obj, dict) else 1
        clock_obj  = p.get("clock", {})
        clock_val  = clock_obj.get("displayValue", "0:00") if isinstance(clock_obj, dict) else "0:00"
        elapsed    = espn_clock_to_seconds(clock_val, pnum)

        team_obj      = p.get("team", {})
        pen_team_abbr = team_obj.get("abbreviation", "").upper() if isinstance(team_obj, dict) else ""

        # Direction: team field = team that COMMITTED the penalty
        # away committed → away short (4) → home PP → 4v5 PP
        # home committed → home short (4) → away PP → 5v4 PP
        if pen_team_abbr == away_abbr.upper():
            sit_label = "4v5 PP"
            pen_side  = "away"
        elif pen_team_abbr == home_abbr.upper():
            sit_label = "5v4 PP"
            pen_side  = "home"
        else:
            sit_label = "PP"
            pen_side  = ""

        penalties.append({
            "elapsed":      elapsed,
            "period":       pnum,
            "duration_sec": duration,
            "sit_label":    sit_label,
            "is_dm":        is_dm,
            "is_major":     is_major,
            "pen_side":     pen_side,
        })
    return penalties

def build_espn_pp_windows(espn_penalties, espn_plays):
    """
    Build PP windows from ESPN penalty data using NHL rules.
    Minor: ends at +120s OR first PP goal.
    Double minor: first goal ends first 2-min half; second half always runs full.
    Major: full 5 min regardless of goals.
    """
    if not espn_penalties:
        return []

    pp_goal_times = []
    for p in espn_plays:
        text      = (p.get("text") or "").lower()
        type_obj  = p.get("type", {})
        type_text = (type_obj.get("text", "") if isinstance(type_obj, dict) else "").lower()
        if "goal" in type_text and "power play" in text:
            period_obj = p.get("period", {})
            pnum       = period_obj.get("number", 1) if isinstance(period_obj, dict) else 1
            clock_obj  = p.get("clock", {})
            clock_val  = clock_obj.get("displayValue", "0:00") if isinstance(clock_obj, dict) else "0:00"
            pp_goal_times.append(espn_clock_to_seconds(clock_val, pnum))
    pp_goal_times.sort()

    windows = []
    for pen in sorted(espn_penalties, key=lambda x: x["elapsed"]):
        start = pen["elapsed"]
        dur   = pen["duration_sec"]
        sit   = pen["sit_label"]
        if pen["is_dm"]:
            mid = start + 120
            end = start + 240
            first_goal = next((t for t in pp_goal_times if start < t <= mid), None)
            if first_goal:
                # Extend by 1s to include the goal-scoring moment in the PP
                windows.append((start, first_goal + 1, sit))
                windows.append((mid, end, sit))
            else:
                windows.append((start, end, sit))
        elif pen["is_major"]:
            windows.append((start, start + dur, sit))
        else:
            end = start + dur
            first_goal = next((t for t in pp_goal_times if start < t <= end), None)
            # Extend by 1s to include the goal-scoring moment in the PP
            end_time = (first_goal + 1) if first_goal else end
            windows.append((start, end_time, sit))

    return windows

# =========================
# SITUATION WINDOW BUILDER
# =========================
def build_situation_windows(nhl_data, espn_plays=None, away_abbr="", home_abbr=""):
    """
    Build gapless situation windows using a hybrid approach.

    PRIMARY: NHL API situationCode (most accurate)
      Phase 1: Build raw windows from every NHL play
      Phase 2: Patch period-start carry-over windows
      Phase 2b: Gap-fill PP windows from NHL penalty data when sit codes missing
      Phase 3: Merge adjacent same-situation windows
      Phase 4: Validate PP windows (penalty required, min duration)

    FALLBACK: ESPN penalty text
      Phase 5: For penalties the NHL API missed entirely, use ESPN penalty
               windows to fill gaps. NHL always wins where both have data.
    """
    nhl_plays      = nhl_data.get("plays", [])
    nhl_penalties  = nhl_data.get("penalties", [])

    # Mark coincidental penalties as no-PP
    nhl_penalties = detect_coincidental_penalties(list(nhl_penalties))

    if not nhl_plays:
        # No NHL data at all — fall back to pure ESPN
        if espn_plays:
            esp_pens = parse_espn_penalties(espn_plays, away_abbr, home_abbr)
            return build_espn_pp_windows(esp_pens, espn_plays)
        return []

    BOUNDARY_TYPES = {
        "period-start", "period-end", "game-start", "game-end",
        "shootout-start", "shootout-end",
    }

    sorted_plays = sorted(
        nhl_plays,
        key=lambda p: (p["period"], p["elapsed"], p["sort_order"])
    )

    # ── Phase 1: raw gapless windows with lag-artifact detection ─────────
    # The NHL API occasionally emits a stale situationCode on a single play
    # just before a real situation change — a lagged "blip" from the previous
    # state. These produce phantom PP/4v5 windows covering 1-20 seconds that
    # Phase 4 passes (a real penalty IS nearby) but the situation was never
    # actually active.
    #
    # Detection: a play is a lag artifact when ALL of:
    #   1. Its sit code differs from both the previous AND next play's codes
    #   2. The previous and next play share the same code (isolated blip)
    #   3. Its duration (distance to next play) is < LAG_MAX_DURATION seconds
    #      — real situations always last ≥ 120s (minor), so < 30s = lag
    LAG_MAX_DURATION = 30  # seconds — any blip shorter than this is lag

    cleaned_plays = []
    n = len(sorted_plays)
    for i, p in enumerate(sorted_plays):
        prev_sit_code = sorted_plays[i-1]["sit_code"] if i > 0 else None
        next_sit_code = sorted_plays[i+1]["sit_code"] if i < n-1 else None
        next_elapsed  = sorted_plays[i+1]["elapsed"]  if i < n-1 else None
        duration      = (next_elapsed - p["elapsed"]) if next_elapsed is not None else 9999
        if (prev_sit_code is not None
                and next_sit_code is not None
                and p["sit_code"] != prev_sit_code
                and p["sit_code"] != next_sit_code
                and prev_sit_code == next_sit_code
                and duration < LAG_MAX_DURATION):
            continue   # lag artifact — skip, don't build a window from this play
        cleaned_plays.append(p)

    raw_windows  = []
    prev_sit     = None
    win_start    = 0
    win_sit_code = ""
    win_sit      = ""
    win_boundary = False

    for p in cleaned_plays:
        sit = p["sit_code"]
        if sit == prev_sit:
            continue
        if prev_sit is not None:
            raw_windows.append([win_start, p["elapsed"], win_sit_code, win_sit, win_boundary])
        win_start    = p["elapsed"]
        win_sit_code = sit
        win_sit      = p["situation"]
        win_boundary = p.get("type_key", "") in BOUNDARY_TYPES
        prev_sit     = sit

    if prev_sit is not None:
        raw_windows.append([win_start, 99999, win_sit_code, win_sit, win_boundary])

    # ── Phase 2: patch period-start carry-over windows ────────────────────
    period_starts = {
        p["elapsed"] for p in sorted_plays
        if p.get("type_key", "") == "period-start"
    }
    for i, win in enumerate(raw_windows):
        w_start, w_end, w_sit_code, w_sit, w_is_boundary = win
        if not w_is_boundary or w_start not in period_starts:
            continue
        next_real = next(
            (raw_windows[j] for j in range(i + 1, len(raw_windows))
             if not raw_windows[j][4]),
            None
        )
        if next_real and next_real[2] != w_sit_code and next_real[0] - w_start <= 60:
            raw_windows[i][3] = next_real[3]

    # ── Phase 2b: gap-fill PP windows from penalty data ────────────────────
    # For every penalty that gave a real PP, check if a PP window covers it.
    # If not, look for ANY play with a PP sit code after the penalty and
    # use it to synthesise the window.
    for pen in nhl_penalties:
        if pen["is_no_pp"]:
            continue
        pen_e   = pen["elapsed"]
        expect_end = pen_e + pen["duration_sec"] + 60   # PP duration + buffer

        covered = any(
            "PP" in w[3] and w[0] <= expect_end and w[1] >= pen_e
            for w in raw_windows
        )
        if covered:
            continue

        first_pp = next(
            (p for p in sorted_plays
             if p["elapsed"] > pen_e
             and p["elapsed"] <= expect_end
             and "PP" in p["situation"]),
            None
        )
        if not first_pp:
            continue

        synth_end = next(
            (p["elapsed"] for p in sorted_plays
             if p["elapsed"] > first_pp["elapsed"] and "PP" not in p["situation"]),
            first_pp["elapsed"] + pen["duration_sec"]
        )

        synth = [first_pp["elapsed"], synth_end,
                 first_pp["sit_code"], first_pp["situation"], False]
        for i, w in enumerate(raw_windows):
            if w[0] >= synth[0]:
                raw_windows.insert(i, synth)
                break
        else:
            raw_windows.append(synth)

    # ── Phase 3: merge adjacent same-situation windows ────────────────────
    merged = []
    for w in raw_windows:
        sit_str = w[3]
        if merged and merged[-1][2] == sit_str:
            merged[-1][1] = w[1]
        else:
            merged.append([w[0], w[1], sit_str])
    merged = [w for w in merged if w[1] > w[0]]

    # ── Phase 4: validate PP windows against NHL rules ─────────────────────
    # Check A: minimum duration 5s (drop near-zero artifacts only)
    # Check B: must have a non-coincidental penalty within lookback range
    MIN_PP_DURATION    = 5
    MAX_PENALTY_LOOKBACK = 360  # 5-min major + buffer

    # Use only non-coincidental penalties for validation
    valid_penalty_times = sorted(
        p["elapsed"] for p in nhl_penalties if not p["is_no_pp"]
    )
    # Also build a map for tail-capping: elapsed → duration_sec
    valid_penalty_map = {
        p["elapsed"]: p["duration_sec"]
        for p in nhl_penalties if not p["is_no_pp"]
    }
    # Maximum allowed PP window duration: 5-min major + 10s buffer
    PP_MAX_CAP = 310

    def has_valid_penalty(win_start):
        lo = win_start - MAX_PENALTY_LOOKBACK
        hi = win_start + 60
        return any(lo <= t <= hi for t in valid_penalty_times)

    def cap_pp_end(win_start, win_end):
        """Cap a PP window end at penalty_start + duration + 10s buffer.
        Prevents Phase 3 merge contamination: a lagged PP-coded play after
        the PP expired (faceoff with stale sit code) extends the merged window
        past the true expiry. Capping at the penalty's known duration stops this.
        Applies the NEAREST matching penalty's duration.
        """
        lo = win_start - MAX_PENALTY_LOOKBACK
        hi = win_start + 60
        candidates = [(et, valid_penalty_map[et])
                      for et in valid_penalty_times
                      if lo <= et <= hi]
        if not candidates:
            return win_end
        pen_et, pen_dur = min(candidates, key=lambda x: abs(x[0] - win_start))
        cap = pen_et + pen_dur + 10
        return min(win_end, cap)

    validated = []
    for w in merged:
        w_start, w_end, w_sit = w
        dur   = w_end - w_start if w_end < 99999 else 9999
        is_pp = "PP" in w_sit
        if is_pp:
            if dur < MIN_PP_DURATION:
                # PP window too short to be real — replace with 5v5.
                # "5v5" not the bare skater count: if no real PP, ice was even
                # strength. Bare "4v5" is misleading and passes the PP filter.
                validated.append([w_start, w_end, "5v5"])
                continue
            if not has_valid_penalty(w_start):
                # No matching penalty found — same reasoning: default to 5v5.
                validated.append([w_start, w_end, "5v5"])
                continue
            # Tail-cap: limit window end to penalty_start + duration + buffer
            # to prevent Phase 3 merge contamination extending past expiry
            capped_end = cap_pp_end(w_start, w_end)
            if capped_end > w_start:
                validated.append([w_start, capped_end, w_sit])
            else:
                validated.append([w_start, w_end, "5v5"])
            continue
        validated.append(list(w))

    # Merge after validation
    final_nhl = []
    for w in validated:
        if final_nhl and final_nhl[-1][2] == w[2]:
            final_nhl[-1][1] = w[1]
        else:
            final_nhl.append(w)
    final_nhl = [(w[0], w[1], w[2]) for w in final_nhl if w[1] > w[0]]

    # ── Phase 5: Authoritative penalty-based override for wrong NHL data ───
    # Build PP windows from BOTH NHL penalty data AND ESPN penalty text,
    # then use these authoritative windows to override any NHL situation
    # window that disagrees during the PP period.
    #
    # Why both sources:
    #   - NHL details.duration is structured/reliable when present
    #   - ESPN text catches penalties NHL API may have missed
    #   - Combining gives maximum coverage
    #
    # Override is necessary (not just gap-fill) because the NHL API
    # occasionally codes goal plays during a brief PP as 5v5, or adds
    # spurious EN tags. The penalty list itself is the ground truth.

    # ── Build authoritative PP windows from NHL penalty details ────────────
    nhl_penalty_windows = []
    if nhl_penalties:
        # Use NHL goals to find PP-ending events (minor ends on goal)
        nhl_pp_goals = sorted([
            p["elapsed"] for p in sorted_plays
            if p.get("type_key") == "goal" and "PP" in (p.get("situation") or "")
        ])

        for pen in nhl_penalties:
            if pen["is_no_pp"]:
                continue
            start = pen["elapsed"]
            dur   = pen["duration_sec"]
            is_maj = pen["duration_min"] == 5
            is_dm  = pen["duration_min"] == 4

            sit_label = ("5v4 PP" if pen["pen_side"] == "away"
                         else "4v5 PP" if pen["pen_side"] == "home"
                         else "PP")

            if is_dm:
                # Double minor: two 2-min segments
                mid = start + 120
                end = start + 240
                first_goal = next(
                    (t for t in nhl_pp_goals if start < t <= mid),
                    None
                )
                if first_goal:
                    # Include the goal moment in the PP window (+1 second)
                    nhl_penalty_windows.append((start, first_goal + 1, sit_label))
                    nhl_penalty_windows.append((mid, end, sit_label))
                else:
                    nhl_penalty_windows.append((start, end, sit_label))
            elif is_maj:
                # Major: full 5 min regardless of goals
                nhl_penalty_windows.append((start, start + dur, sit_label))
            else:
                # Minor: ends at duration OR first PP goal
                # Include the goal moment in the PP window (+1 second)
                end = start + dur
                first_goal = next(
                    (t for t in nhl_pp_goals if start < t <= end),
                    None
                )
                end_time = (first_goal + 1) if first_goal else end
                nhl_penalty_windows.append((start, end_time, sit_label))

    # ── Build PP windows from ESPN penalty text ────────────────────────────
    espn_wins = []
    if espn_plays and (away_abbr or home_abbr):
        esp_pens = parse_espn_penalties(espn_plays, away_abbr, home_abbr)
        espn_wins = build_espn_pp_windows(esp_pens, espn_plays)

    # ── Merge authoritative windows from both sources ──────────────────────
    # Strategy: any time covered by either source is treated as authoritative.
    # If both agree, fine. If they disagree, prefer the NHL source (more
    # specific situation code via duration).
    authoritative = list(nhl_penalty_windows)

    for (es, ee, esit) in espn_wins:
        # Skip if NHL penalty windows already cover this range
        covered_by_nhl = any(
            ns <= es + 5 and ne >= ee - 5
            for (ns, ne, _) in nhl_penalty_windows
        )
        if covered_by_nhl:
            continue
        authoritative.append((es, ee, esit))

    authoritative.sort(key=lambda w: w[0])

    # ── Override Phase 4 windows with authoritative penalty windows ────────
    # For each authoritative PP window, override any overlapping NHL
    # situation window that says 5v5 or has implausible EN tags during
    # what should be a PP.
    nhl_windows = [list(w) for w in final_nhl]

    for (es, ee, esit) in authoritative:
        # Fix 2: apply tail-cap to authoritative window before insertion.
        # Phase 4 already caps sit-code windows; Phase 5 must obey the same
        # rule so inserted windows cannot run past the penalty true expiry.
        ee = cap_pp_end(es, ee)
        if ee <= es:
            continue  # cap eliminated window entirely — skip

        new_windows = []
        for (ns, ne, nsit) in nhl_windows:
            if ne <= es or ns >= ee:
                # No overlap → keep as-is
                new_windows.append([ns, ne, nsit])
                continue

            # Fix 1+3: preserve any window that already carries a directional
            # PP label (" PP" with a leading space).
            #   "5v4 PP", "4v5 PP", "5v3 PP", "4v3 PP" → kept (directional)
            #   "6v5 Away EN PP", "5v6 Home EN PP"      → kept (EN+PP, more specific)
            #   "PP"                                     → overridden (generic)
            #   "6v5 Away EN" (bare EN, no PP)           → overridden (less specific)
            if " PP" in nsit:
                new_windows.append([ns, ne, nsit])
                continue

            # Override the overlapping portion:
            #   pre  = NHL portion before authoritative window starts
            #   mid  = overlap → replaced with authoritative label
            #   post = NHL portion after authoritative window ends
            pre_start, pre_end   = ns, max(ns, es)
            mid_start, mid_end   = max(ns, es), min(ne, ee)
            post_start, post_end = min(ne, ee), ne

            if pre_end > pre_start:
                new_windows.append([pre_start, pre_end, nsit])
            if mid_end > mid_start:
                new_windows.append([mid_start, mid_end, esit])
            if post_end > post_start:
                new_windows.append([post_start, post_end, nsit])

        nhl_windows = new_windows

    # Sort and re-merge adjacent same-situation windows after overrides
    nhl_windows.sort(key=lambda w: w[0])
    re_merged = []
    for w in nhl_windows:
        if re_merged and re_merged[-1][2] == w[2]:
            re_merged[-1][1] = w[1]
        else:
            re_merged.append(list(w))
    final_nhl = [(w[0], w[1], w[2]) for w in re_merged if w[1] > w[0]]

    # ── Phase 6: sit code scan — recover direction on remaining generic PP ─
    # Any window still labelled "PP" after Phase 5 has no direction from
    # either penalty source. The NHL plays inside the window carry directional
    # sit codes — the same codes Phase 1 already processed correctly.
    # Read the first play sit_code inside each generic window and derive
    # direction via parse_nhl_situation(). Guard: only apply if " PP" in
    # result (directional). Skips silently when no play is found.
    final_nhl   = list(final_nhl)
    opt_c_log   = []   # diagnostic: one entry per generic PP window examined

    for i, (ws, we, wsit) in enumerate(final_nhl):
        if wsit != "PP":
            continue
        first_play = next(
            (p for p in sorted_plays if ws <= p["elapsed"] < we),
            None
        )
        if first_play:
            sit_code = first_play["sit_code"]
            derived  = parse_nhl_situation(sit_code)
            if " PP" in derived:
                final_nhl[i] = (ws, we, derived)
                opt_c_log.append({
                    "ws": ws, "we": we,
                    "before": "PP", "after": derived,
                    "sit_code": sit_code, "elapsed": first_play["elapsed"],
                    "converted": True,
                })
            else:
                opt_c_log.append({
                    "ws": ws, "we": we,
                    "before": "PP", "after": "PP",
                    "sit_code": sit_code or "—", "elapsed": first_play["elapsed"],
                    "converted": False,
                })
        else:
            opt_c_log.append({
                "ws": ws, "we": we,
                "before": "PP", "after": "PP",
                "sit_code": "—", "elapsed": "—",
                "converted": False,
            })

    # Attach log to nhl_data so the diagnostic expander can read it
    nhl_data["_opt_c_log"] = opt_c_log

    return final_nhl

def find_nhl_situation(espn_play, windows):
    """Find on-ice situation for an ESPN play from situation windows."""
    if not windows:
        return ""
    elapsed = espn_play.get("elapsed", 0)
    for (ws, we, wsit) in windows:
        if ws <= elapsed < we:
            return wsit
    if windows and windows[-1][0] <= elapsed < windows[-1][1]:
        return windows[-1][2]
    best, best_gap = None, FUZZY_SECONDS + 1
    for (ws, we, wsit) in windows:
        gap = min(abs(elapsed - ws), abs(elapsed - we))
        if gap < best_gap:
            best_gap, best = gap, wsit
    return best or ""

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
    # Return cached plays if available — last_refresh only updates on real fetch
    if st.session_state.cached_event_id == event_id and st.session_state.cached_plays:
        return st.session_state.cached_plays

    try:
        resp = requests.get(ESPN_SUMMARY, params={"event": event_id}, timeout=15)
        resp.raise_for_status()
        raw_plays = resp.json().get("plays", [])
    except Exception as e:
        st.error(f"ESPN error: {e}")
        return []

    # Resolve cache bucket: Refresh button sets force_bucket to current+1
    # to guarantee a cache miss. Normal loads use 30-second time buckets.
    bucket = st.session_state.get("force_bucket") or int(time.time() // 30)
    nhl_data = fetch_nhl_plays(nhl_game_id, cache_bucket=bucket) if nhl_game_id else {"plays": [], "penalties": [], "teams": {}}
    windows  = build_situation_windows(nhl_data, espn_plays=raw_plays,
                                        away_abbr=away_abbr, home_abbr=home_abbr)

    # last_refresh only updates after a real fetch completes
    st.session_state.last_refresh = datetime.now(ET)

    plays = []
    for p in raw_plays:
        period_obj = p.get("period", {})
        pnum       = period_obj.get("number", 1) if isinstance(period_obj, dict) else 1
        ptype      = period_obj.get("type", "")  if isinstance(period_obj, dict) else ""
        clock_obj  = p.get("clock", {})
        clock_val  = clock_obj.get("displayValue", "") if isinstance(clock_obj, dict) else str(clock_obj)
        type_obj   = p.get("type", {})
        type_text  = type_obj.get("text", "") if isinstance(type_obj, dict) else str(type_obj)
        type_id    = type_obj.get("id",   "") if isinstance(type_obj, dict) else ""
        text       = p.get("text", "")
        wall_raw   = p.get("wallclock", "")
        seq        = int(p.get("sequenceNumber", 0))
        elapsed    = espn_clock_to_seconds(clock_val, pnum)

        team_obj   = p.get("team", {})
        pen_team   = team_obj.get("abbreviation", "").upper() if isinstance(team_obj, dict) else ""

        situation = find_nhl_situation({"elapsed": elapsed}, windows)

        plays.append({
            "seq":          seq,
            "period_num":   pnum,
            "period_type":  ptype,
            "period_label": period_label(pnum, ptype),
            "clock":        clock_val,
            "elapsed":      elapsed,
            "type_text":    type_text,
            "type_id":      type_id,
            "pen_team":     pen_team,
            "text":         text,
            "situation":    situation,
            "wall_raw":     wall_raw,
            "wall_et":      fmt_et_full(wall_raw),
            "wall_dt":      to_et(wall_raw),
            "away_score":   p.get("awayScore", ""),
            "home_score":   p.get("homeScore", ""),
            "emoji":        get_play_emoji(type_text),
            "is_pp_cause":  False,
            "pp_arrow":     "",
        })

    plays.sort(key=lambda x: x["seq"])

    # ── Tag penalty plays that caused a confirmed PP ───────────────────────
    # Option E pipeline — two stages:
    #
    # Stage 1A — Mirror text detection (coincidental filter):
    #   Coincidental pairs in ESPN always appear as text mirrors:
    #   "A [infraction] against B" paired with "B [infraction] against A".
    #   Detected by requiring ALL words after "against" in play X to appear
    #   before "against" in play Y and vice versa (full name crossover).
    #   Mirror pairs at same elapsed (±5s) are marked is_coincidental=True.
    #
    # Stage 1B — One-NHL-penalty-one-tag (±90s, direction check):
    #   Each non-coincidental NHL penalty can be claimed by at most ONE ESPN
    #   play. Once claimed, subsequent plays at the same time get no match.
    #   Tolerance raised to ±90s: ESPN logs at infraction, NHL at faceoff.
    #   Direction check (nhl_side → expected committer) applied when team known.
    #
    # Stage 2 — Condition A fallback (inside PP window):
    #   For penalties with no NHL data (e.g. very short PPs like Thompson),
    #   parse_espn_penalties builds ESPN PP windows. Condition A catches them.

    def _is_mirror(text_a, text_b):
        """Coincidental mirror: ALL words after 'against' in A must appear
        before 'against' in B, and vice versa. Requires full name crossover —
        resistant to partial/generic word matches."""
        a, b = text_a.lower(), text_b.lower()
        if " against " not in a or " against " not in b:
            return False
        a_before, a_after = a.split(" against ", 1)
        b_before, b_after = b.split(" against ", 1)
        a_after_words  = set(a_after.split())
        b_after_words  = set(b_after.split())
        a_before_words = set(a_before.split())
        b_before_words = set(b_before.split())
        forward  = bool(a_after_words) and a_after_words <= b_before_words
        backward = bool(b_after_words) and b_after_words <= a_before_words
        return forward and backward

    nhl_pen_data = nhl_data.get("penalties", [])
    pp_nhl_pens  = {pen["elapsed"]: pen
                    for pen in nhl_pen_data if not pen.get("is_no_pp", True)}

    def _pen_arrow(pen_side):
        # pen_side = team that DREW/benefited from the penalty (NHL convention)
        # away drew → home committed → home short → 5v4 PP
        # home drew → away committed → away short → 4v5 PP
        if pen_side == "away":  return "5v5 → 5v4 PP"
        if pen_side == "home":  return "5v5 → 4v5 PP"
        return "5v5 → PP"

    pp_windows = [(ws, we, wsit) for (ws, we, wsit) in windows if "PP" in wsit]

    # Stage 1A: detect coincidental mirror pairs
    pen_plays = [(i, p) for i, p in enumerate(plays) if is_espn_penalty(p["type_id"])]
    for ii, (i, p1) in enumerate(pen_plays):
        for jj, (j, p2) in enumerate(pen_plays):
            if ii >= jj:
                continue
            if abs(p1["elapsed"] - p2["elapsed"]) > 5:
                continue
            if _is_mirror(p1.get("text", ""), p2.get("text", "")):
                plays[i]["is_coincidental"] = True
                plays[j]["is_coincidental"] = True

    # Diagnostic capture
    _diag = {
        "pp_windows":    list(pp_windows),
        "nhl_penalties": [{"elapsed":       et,
                           "pen_side":      p["pen_side"],
                           "desc":          p.get("desc_key", ""),
                           "dur_min":       p.get("duration_min", "?"),
                           "diag_source":   p.get("diag_source", "—"),
                           "diag_a":        p.get("diag_a", "—"),
                           "diag_d":        p.get("diag_d", "—"),
                           "committed_id":  p.get("committed_id") or "—",
                           "drawn_id":      p.get("drawn_id") or "—"}
                          for et, p in sorted(pp_nhl_pens.items())],
        "penalty_plays": [],
        # rosterSpots availability
        "roster_spots_count": nhl_data.get("roster_spots_raw", "not in response"),
        "roster_map_size":    len(nhl_data.get("roster_map", {})),
        # Option C conversion log from Phase 6
        "opt_c_log":          nhl_data.get("_opt_c_log", []),
    }

    claimed_nhl = set()  # elapsed values of NHL penalties already claimed

    for play in plays:
        if not is_espn_penalty(play["type_id"]):
            continue
        if play.get("is_coincidental"):
            _diag["penalty_plays"].append({
                "clock": play["clock"], "period": play["period_label"],
                "type": play["type_text"], "type_id": play["type_id"],
                "elapsed": play["elapsed"], "tagged": False,
                "reason": "mirror coincidental — skipped",
            })
            continue

        pel            = play["elapsed"]
        pen_team       = play["pen_team"]
        committed_side = ("away" if pen_team == away_abbr.upper() else
                          "home" if pen_team == home_abbr.upper() else "")
        match_reason   = "no match"



        # Stage 1B: primary NHL ±90s, direction check, one-claim per penalty
        best_pen, best_gap = None, 91
        for et, pen in pp_nhl_pens.items():
            if et in claimed_nhl:
                continue
            gap = abs(pel - et)
            if gap >= best_gap:
                continue
            nhl_side = pen["pen_side"]
            expected_committer = "away" if nhl_side == "home" else "home"
            if committed_side and committed_side != expected_committer:
                continue
            best_gap, best_pen = gap, (et, pen)

        if best_pen:
            claimed_nhl.add(best_pen[0])
            play["is_pp_cause"] = True
            play["pp_arrow"]    = _pen_arrow(best_pen[1]["pen_side"])
            match_reason = f"NHL primary {best_gap}s (claimed)"
        else:
            # Stage 2: Condition A only — inside PP window
            for ws, we, wsit in pp_windows:
                if ws <= pel < we:
                    play["is_pp_cause"] = True
                    play["pp_arrow"]    = f"5v5 → {wsit}"
                    match_reason = f"Cond A inside [{ws}-{we}]"
                    break

        _diag["penalty_plays"].append({
            "clock":      play["clock"], "period": play["period_label"],
            "type":       play["type_text"], "type_id": play["type_id"],
            "elapsed":    pel, "tagged": play["is_pp_cause"], "reason": match_reason,
            "espn_team":  pen_team or "—",
        })

    st.session_state["_pen_diag"] = _diag

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
        f"📡 NHL `{nhl_id}` + ESPN hybrid" if nhl_id
        else "📡 ESPN only — NHL ID not found"
    )

    # ── Penalty tagging diagnostic ────────────────────────────────────────
    # Four sections:
    #   1. PP windows — directional vs generic
    #   2. NHL penalties — direction source + A/D roster lookup readiness
    #   3. Option C — Phase 6 sit code conversions
    #   4. ESPN penalty plays — match reason
    diag = st.session_state.get("_pen_diag", {})
    if diag:
        with st.expander("🔬 Penalty tagging diagnostic", expanded=False):

            # ── Section 1: PP windows ──────────────────────────────────────
            pp_wins = diag.get("pp_windows", [])
            directional = sum(1 for _,_,s in pp_wins if " PP" in s)
            generic     = sum(1 for _,_,s in pp_wins if s == "PP")
            st.markdown(
                f"**PP windows ({len(pp_wins)}) — "
                f"{directional} directional ✅ · {generic} generic ⚠️:**"
            )
            for ws, we, wsit in pp_wins[:30]:
                icon = "✅" if " PP" in wsit else "⚠️"
                dur  = we - ws
                st.write(f"  {icon} [{ws}s – {we}s] `{wsit}` ({dur}s)")

            st.divider()

            # ── Section 2: NHL penalties — direction source + A/D readiness ─
            nhl_pens     = diag.get("nhl_penalties", [])
            roster_count = diag.get("roster_spots_count", "?")
            roster_size  = diag.get("roster_map_size", 0)
            st.markdown(f"**NHL PP-causing penalties ({len(nhl_pens)}):**")

            if roster_count == 0 or roster_size == 0:
                st.warning(
                    f"⚠️ rosterSpots: {roster_count} entries — "
                    f"Options A+D fallback unavailable for this game"
                )
            else:
                st.success(
                    f"✅ rosterSpots: {roster_count} entries, "
                    f"{roster_size} valid — Options A+D fallback ready"
                )

            if nhl_pens:
                missing = sum(1 for np in nhl_pens if not np["pen_side"])
                if missing:
                    st.warning(f"⚠️ {missing} penalt{'y' if missing==1 else 'ies'} "
                               f"with pen_side unresolved")
                for np in nhl_pens:
                    side = np["pen_side"] or "❌ MISSING"
                    src  = np.get("diag_source", "—")
                    # Roster readiness: show whether A+D WOULD resolve if needed
                    a_res  = np.get("diag_a", "—")
                    d_res  = np.get("diag_d", "—")
                    a_rdy  = "✅" if a_res in ("away","home") else "—"
                    d_rdy  = "✅" if d_res in ("away","home") else "—"
                    cid    = np.get("committed_id","—")
                    did    = np.get("drawn_id","—")
                    st.write(
                        f"  elapsed={np['elapsed']}s | {np['desc']} {np['dur_min']}min"
                        f" | side={side} via {src}"
                        f" | A-ready={a_rdy}({cid}→{a_res})"
                        f" | D-ready={d_rdy}({did}→{d_res})"
                    )
            else:
                st.warning("No NHL PP-causing penalties found")

            st.divider()

            # ── Section 3: Option C conversions (Phase 6 sit code scan) ────
            opt_c = diag.get("opt_c_log", [])
            converted = [e for e in opt_c if e["converted"]]
            skipped   = [e for e in opt_c if not e["converted"]]
            st.markdown(
                f"**Option C — Phase 6 sit code scan "
                f"({len(converted)} converted · {len(skipped)} skipped):**"
            )
            if not opt_c:
                st.write("  No generic PP windows remained after Phase 5")
            for e in opt_c:
                icon = "✅" if e["converted"] else "⚠️"
                sc   = e["sit_code"]
                el   = e["elapsed"]
                after = e["after"]
                reason = (f"sit={sc} at {el}s → {after}"
                          if e["converted"]
                          else f"sit={sc} at {el}s → no PP direction")
                st.write(
                    f"  {icon} [{e['ws']}s–{e['we']}s] PP → {after} | {reason}"
                )

            st.divider()

            # ── Section 4: ESPN penalty plays ──────────────────────────────
            pens = diag.get("penalty_plays", [])
            tagged   = sum(1 for p in pens if p["tagged"])
            untagged = len(pens) - tagged
            st.markdown(
                f"**ESPN penalty plays ({len(pens)}) — "
                f"{tagged} tagged ✅ · {untagged} untagged ❌:**"
            )
            for p in pens:
                tag_icon = "✅" if p["tagged"] else "❌"
                st.write(
                    f"  {tag_icon} {p['period']} {p['clock']} | "
                    f"id=`{p['type_id']}` `{p['type']}` elapsed={p['elapsed']}s"
                    f" → {p['reason']}"
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
                if USE_PP_FILTER and " PP" not in sit and not p.get("is_pp_cause", False): return False
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
        else:
            # Standard render — identical to original
            st.subheader(f"{emoji} {p.get('period_label')} | ⏱️ {p.get('clock')}")
            st.markdown(f"📊 **Score:** {p.get('away_score')} - {p.get('home_score')}")
            st.markdown(f"🎯 **Event:** {p.get('type_text')}")
            if sit:
                # Generic "PP" (no direction) means ESPN team field was absent.
                # Display as blank rather than the misleading "PP" label.
                sit_display = "" if sit == "PP" else sit
                if sit_display:
                    st.markdown(f"⚖️ **Strength:** `{sit_display}`")
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
