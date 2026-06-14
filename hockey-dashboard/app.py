from flask import Flask, redirect, url_for, session, request, render_template, flash
from functools import wraps
from datetime import datetime, timedelta
import os
from dotenv import load_dotenv
from yahoo_client import YahooClient

load_dotenv()

app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", os.urandom(24).hex())

CLIENT_ID = os.getenv("YAHOO_CLIENT_ID")
CLIENT_SECRET = os.getenv("YAHOO_CLIENT_SECRET")
REDIRECT_URI = os.getenv("REDIRECT_URI", "http://localhost:5000/callback")


def yahoo():
    return YahooClient(CLIENT_ID, CLIENT_SECRET)


def login_required(f):
    @wraps(f)
    def wrapped(*args, **kwargs):
        if "access_token" not in session:
            flash("Connect your Yahoo account to continue.", "info")
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return wrapped


def auto_refresh(f):
    """Silently refresh the Yahoo access token if it's expired."""
    @wraps(f)
    def wrapped(*args, **kwargs):
        expiry_str = session.get("token_expiry")
        if expiry_str and "refresh_token" in session:
            if datetime.now() >= datetime.fromisoformat(expiry_str):
                try:
                    tokens = yahoo().refresh_token(session["refresh_token"])
                    session["access_token"] = tokens["access_token"]
                    session["refresh_token"] = tokens.get("refresh_token", session["refresh_token"])
                    session["token_expiry"] = (
                        datetime.now() + timedelta(seconds=tokens.get("expires_in", 3600))
                    ).isoformat()
                except Exception:
                    session.clear()
                    flash("Session expired. Please reconnect.", "warning")
                    return redirect(url_for("login"))
        return f(*args, **kwargs)
    return wrapped


# ── Auth ──────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    if "access_token" not in session:
        return render_template("login.html", auth_url=yahoo().get_auth_url(REDIRECT_URI))
    return redirect(url_for("leagues"))


@app.route("/login")
def login():
    if "access_token" in session:
        return redirect(url_for("leagues"))
    return render_template("login.html", auth_url=yahoo().get_auth_url(REDIRECT_URI))


@app.route("/callback")
def callback():
    error = request.args.get("error")
    if error:
        flash(f"Yahoo authorization failed: {error}", "danger")
        return redirect(url_for("login"))

    code = request.args.get("code")
    if not code:
        flash("No authorization code received.", "danger")
        return redirect(url_for("login"))

    try:
        tokens = yahoo().exchange_code(code, REDIRECT_URI)
        session["access_token"] = tokens["access_token"]
        session["refresh_token"] = tokens.get("refresh_token")
        session["token_expiry"] = (
            datetime.now() + timedelta(seconds=tokens.get("expires_in", 3600))
        ).isoformat()
        flash("Connected to Yahoo Fantasy!", "success")
        return redirect(url_for("leagues"))
    except Exception as e:
        flash(f"Connection failed: {e}", "danger")
        return redirect(url_for("login"))


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


# ── Leagues ───────────────────────────────────────────────────────────────────

@app.route("/leagues")
@login_required
@auto_refresh
def leagues():
    try:
        all_leagues = yahoo().get_leagues(session["access_token"])
        return render_template("leagues.html", leagues=all_leagues)
    except Exception as e:
        flash(f"Could not load leagues: {e}", "danger")
        return render_template("leagues.html", leagues=[])


# ── Dashboard ─────────────────────────────────────────────────────────────────

@app.route("/dashboard/<league_key>")
@login_required
@auto_refresh
def dashboard(league_key):
    token = session["access_token"]
    client = yahoo()
    try:
        league = client.get_league_info(token, league_key)
        my_team = client.get_my_team(token, league_key)
        if not my_team:
            flash("Your team was not found in this league.", "warning")
            return redirect(url_for("leagues"))

        week = request.args.get("week", league.get("current_week"))
        stat_cats = client.get_stat_categories(token, league_key)
        matchup = client.get_current_matchup(token, my_team["team_key"], week)

        return render_template(
            "dashboard.html",
            league=league,
            my_team=my_team,
            matchup=matchup,
            stat_cats=stat_cats,
            week=week,
            league_key=league_key,
        )
    except Exception as e:
        flash(f"Dashboard error: {e}", "danger")
        return redirect(url_for("leagues"))


# ── Matchup detail ────────────────────────────────────────────────────────────

@app.route("/matchup/<league_key>")
@login_required
@auto_refresh
def matchup(league_key):
    token = session["access_token"]
    client = yahoo()
    try:
        league = client.get_league_info(token, league_key)
        my_team = client.get_my_team(token, league_key)
        week = request.args.get("week", league.get("current_week"))

        matchup_data = client.get_matchup_rosters(
            token, league_key, my_team["team_key"], week
        )

        return render_template(
            "matchup.html",
            league=league,
            my_team=my_team,
            matchup=matchup_data,
            week=week,
            league_key=league_key,
        )
    except Exception as e:
        flash(f"Matchup error: {e}", "danger")
        return redirect(url_for("dashboard", league_key=league_key))


# ── Projections ───────────────────────────────────────────────────────────────

@app.route("/projections/<league_key>")
@login_required
@auto_refresh
def projections(league_key):
    token = session["access_token"]
    client = yahoo()
    try:
        league = client.get_league_info(token, league_key)
        my_team = client.get_my_team(token, league_key)
        week = request.args.get("week", league.get("current_week"))
        team_key = my_team["team_key"]

        stat_cats = client.get_stat_categories(token, league_key)

        week_players = client.get_roster_stats(token, team_key, week, stat_type="week")
        proj_players = client.get_roster_stats(token, team_key, week, stat_type="projected_week")
        season_stats = client.get_season_roster_stats(token, team_key)

        # Merge by player_key
        proj_by_key = {p["player_key"]: p["stats"] for p in proj_players}
        season_by_key = season_stats

        for p in week_players:
            pk = p["player_key"]
            p["projected_stats"] = proj_by_key.get(pk, {})
            p["season_stats"] = season_by_key.get(pk, {})

        return render_template(
            "projections.html",
            league=league,
            my_team=my_team,
            players=week_players,
            stat_cats=stat_cats,
            week=week,
            league_key=league_key,
        )
    except Exception as e:
        flash(f"Projections error: {e}", "danger")
        return redirect(url_for("dashboard", league_key=league_key))


# ── Waiver wire ───────────────────────────────────────────────────────────────

@app.route("/waiver/<league_key>")
@login_required
@auto_refresh
def waiver(league_key):
    token = session["access_token"]
    client = yahoo()
    sort = request.args.get("sort", "AR")
    position = request.args.get("position", "")
    try:
        league = client.get_league_info(token, league_key)
        my_team = client.get_my_team(token, league_key)
        week = league.get("current_week")

        stat_cats = client.get_stat_categories(token, league_key)
        free_agents = client.get_free_agents(token, league_key, sort=sort, position=position)
        matchup = client.get_current_matchup(token, my_team["team_key"], week)

        # Find categories where we're currently losing → highlight in waiver view
        losing_cats = set()
        if matchup and matchup.get("teams"):
            my_t = next((t for t in matchup["teams"] if t["is_mine"]), None)
            opp_t = next((t for t in matchup["teams"] if not t["is_mine"]), None)
            if my_t and opp_t:
                for sid, cat in stat_cats.items():
                    my_val = my_t["stats"].get(sid)
                    opp_val = opp_t["stats"].get(sid)
                    try:
                        mv, ov = float(my_val), float(opp_val)
                        higher_better = cat["sort_order"] == "1"
                        if (higher_better and mv < ov) or (not higher_better and mv > ov):
                            losing_cats.add(sid)
                    except (TypeError, ValueError):
                        pass

        return render_template(
            "waiver.html",
            league=league,
            my_team=my_team,
            free_agents=free_agents,
            stat_cats=stat_cats,
            matchup=matchup,
            losing_cats=losing_cats,
            sort=sort,
            position=position,
            week=week,
            league_key=league_key,
        )
    except Exception as e:
        flash(f"Waiver error: {e}", "danger")
        return redirect(url_for("dashboard", league_key=league_key))


if __name__ == "__main__":
    app.run(debug=True, port=5000)
