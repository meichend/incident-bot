import config
import logging
import logging.config
import sys

from bot.api.flask import app
from bot.models.pg import db_verify, OperationalData, Session
from bot.scheduler import scheduler
from bot.slack.client import (
    slack_workspace_id,
    check_bot_user_in_digest_channel,
)
from bot.slack.handler import app as slack_app
from slack_bolt.adapter.socket_mode import SocketModeHandler
from waitress import serve

logger = logging.getLogger(__name__)
logging.basicConfig(stream=sys.stdout, level=config.log_level)

"""
Check for required environment variables first
"""

if __name__ == "__main__":
    # Pre-flight checks
    ## Check for environment variables
    config.env_check(
        required_envs=[
            "POSTGRES_DB",
            "POSTGRES_HOST",
            "POSTGRES_PASSWORD",
            "POSTGRES_PORT",
            "POSTGRES_USER",
            "SLACK_APP_TOKEN",
            "SLACK_BOT_TOKEN",
            "SLACK_USER_TOKEN",
        ]
    )

"""
Scheduler
"""

scheduler.process.start()

"""
Startup
"""


def db_check():
    logger.info("Testing the database connection...")
    db_info = f"""
------------------------------
Database host:  {config.database_host}
Database port:  {config.database_port}
Database user:  {config.database_user}
Database name:  {config.database_name}
------------------------------
    """
    print(db_info)
    if not db_verify():
        logger.fatal(
            "Cannot connect to the database - check settings and try again."
        )
        sys.exit(1)


def startup_tasks():
    """Tasks that should be run at each startup"""
    from bot.scheduler.scheduler import update_slack_user_list

    # If the init job has already run, skip it
    logger.info("Running startup tasks...")

    # Populate list of Slack users
    update_slack_user_list()

    # Integration Tests
    # --------------------
    if "confluence" in config.active.integrations.get("atlassian"):
        from bot.confluence.api import ConfluenceApi

        api_test = ConfluenceApi()
        passes = api_test.test()
        if not passes:
            logger.fatal(
                "Could not verify Confluence parent page exists.\nYou provided: {}/{}".format(
                    config.active.integrations.get("atlassian")
                    .get("confluence")
                    .get("space"),
                    config.active.integrations.get("atlassian")
                    .get("confluence")
                    .get("parent"),
                )
            )
            sys.exit(1)

    if "jira" in config.active.integrations.get("atlassian"):
        from bot.jira.api import JiraApi

        api_test = JiraApi()
        passes = api_test.test()
        if not passes:
            logger.fatal(
                "Could not verify Jira project exists.\nYou provided: {}".format(
                    config.active.integrations.get("atlassian")
                    .get("jira")
                    .get("project"),
                )
            )
            sys.exit(1)

    if "pagerduty" in config.active.integrations:
        from bot.pagerduty.api import PagerDutyAPI

        if PagerDutyAPI().test() is None:
            sys.exit(1)

        from bot.scheduler.scheduler import update_pagerduty_oc_data

        update_pagerduty_oc_data()
        try:
            if (
                not Session.query(OperationalData)
                .filter(OperationalData.id == "auto_page_teams")
                .all()
            ):
                auto_page_teams = OperationalData(
                    id="auto_page_teams",
                    json_data={"teams": []},
                )
                Session.add(auto_page_teams)
                Session.commit()
        except Exception as error:
            logger.error(f"Error storing auto_page_teams: {error}")
        finally:
            Session.close()
            Session.remove()


if __name__ == "__main__":
    ## Make sure database connection works
    db_check()

    ## Run startup tasks
    startup_tasks()

    ## Startup splash for confirming key options
    startup_message = config.startup_message(workspace=slack_workspace_id)
    print(startup_message)

    # Serve Slack Bolt app
    handler = SocketModeHandler(slack_app, config.slack_app_token)
    handler.connect()

    # Make sure bot user is always present in incident digest channel
    check_bot_user_in_digest_channel()

    # Serve Flask app
    serve(app, host="0.0.0.0", port=3000)
