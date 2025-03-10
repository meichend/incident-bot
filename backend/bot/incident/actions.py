import config
import logging
import re
import slack_sdk.errors
import variables

from bot.audit import log
from bot.incident.action_parameters import (
    ActionParametersSlack,
    ActionParametersWeb,
)
from bot.models.incident import (
    db_read_incident,
    db_update_incident_rca_col,
    db_update_incident_role,
    db_update_incident_status_col,
    db_update_incident_severity_col,
    db_update_incident_updated_at_col,
)
from bot.scheduler import scheduler
from bot.shared import tools
from bot.slack.client import (
    slack_web_client,
    get_formatted_channel_history,
    get_message_content,
    invite_user_to_channel,
    slack_workspace_id,
)
from bot.slack.incident_logging import read as read_incident_pinned_items
from bot.templates.incident.digest_notification import (
    IncidentChannelDigestNotification,
)
from bot.templates.incident.resolution_message import IncidentResolutionMessage
from bot.templates.incident.updates import IncidentUpdate
from bot.templates.incident.user_dm import IncidentUserNotification
from typing import Any, Dict

logger = logging.getLogger("incident.actions")


"""
Functions for handling inbound actions
"""


async def archive_incident_channel(
    action_parameters: type[ActionParametersSlack],
):
    """When an incoming action is incident.archive_incident_channel, this method
    archives the target channel.

    Keyword arguments:
    action_parameters -- type[ActionParametersSlack] containing Slack actions data
    """
    incident_data = db_read_incident(
        channel_id=action_parameters.channel_details["id"]
    )
    try:
        logger.info(f"Archiving {incident_data.channel_name}.")
        result = slack_web_client.conversations_archive(
            channel=incident_data.channel_id
        )
        logger.debug(f"\n{result}\n")
    except slack_sdk.errors.SlackApiError as error:
        logger.error(f"Error archiving {incident_data.channel_name}: {error}")
    finally:
        # Write audit log
        log.write(
            incident_id=incident_data.channel_name,
            event="Channel archived.",
        )


async def assign_role(
    action_parameters: type[ActionParametersSlack] = ActionParametersSlack,
    web_data: type[ActionParametersWeb] = ActionParametersWeb,
    request_origin: str = "slack",
):
    """When an incoming action is incident.assign_role, this method
    assigns the role to the user provided in the input

    Keyword arguments:
    action_parameters(type[ActionParametersSlack]) containing Slack actions data
    web_data(Dict) - if executing from "web", this data must be passed
    request_origin(str) - can either be "slack" or "web"
    """
    match request_origin:
        case "slack":
            try:
                incident_data = db_read_incident(
                    channel_id=action_parameters.channel_details["id"]
                )
                # Target incident channel
                target_channel = incident_data.channel_id
                channel_name = incident_data.channel_name
                user_id = action_parameters.actions["selected_user"]
                action_value = "_".join(
                    action_parameters.actions["block_id"].split("_")[1:3]
                )
                # Find the index of the block that contains info on
                # the role we want to update and format it with the new user later
                blocks = action_parameters.message_details["blocks"]
                index = tools.find_index_in_list(
                    blocks, "block_id", f"role_{action_value}"
                )
                temp_new_role_name = action_value.replace("_", " ")
                target_role = action_value
                ts = action_parameters.message_details["ts"]
            except Exception as error:
                logger.error(
                    f"Error processing incident user update from Slack: {error}"
                )
        case "web":
            try:
                incident_data = db_read_incident(
                    channel_id=web_data.channel_id
                )
                # Target incident channel
                target_channel = incident_data.channel_id
                channel_name = incident_data.channel_name
                user_id = web_data.user
                # Find the index of the block that contains info on
                # the role we want to update and format it with the new user later
                blocks = get_message_content(
                    conversation_id=web_data.channel_id,
                    ts=web_data.bp_message_ts,
                )["blocks"]
                index = tools.find_index_in_list(
                    blocks, "block_id", f"role_{web_data.role}"
                )
                temp_new_role_name = web_data.role.replace("_", " ")
                target_role = web_data.role
                ts = web_data.bp_message_ts
            except Exception as error:
                logger.error(
                    f"Error processing incident user update from web: {error}"
                )

    new_role_name = temp_new_role_name.title()
    blocks[index]["text"]["text"] = f"*{new_role_name}*:\n <@{user_id}>"
    # Convert user ID to user name to use later.
    user_name = next(
        (
            u["name"]
            for u in slack_web_client.users_list()["members"]
            if u["id"] == user_id
        ),
        None,
    )

    try:
        # Update the message
        slack_web_client.chat_update(
            channel=target_channel,
            ts=ts,
            blocks=blocks,
            text=f"{user_id} is now {new_role_name}",
        )
    except Exception as error:
        logger.error(
            f"Error updating channel message during user update: {error}"
        )

    # Send update notification message to incident channel
    try:
        result = slack_web_client.chat_postMessage(
            **IncidentUpdate.role(
                channel=target_channel, role=new_role_name, user=user_id
            ),
            text=f"{user_id} is now {new_role_name}",
        )

        logger.debug(f"\n{result}\n")
    except slack_sdk.errors.SlackApiError as error:
        logger.error(
            f"Error sending role update to the incident channel: {error}"
        )

    # Let the user know they've been assigned the role and what to do
    try:
        result = slack_web_client.chat_postMessage(
            **IncidentUserNotification.create(
                user=user_id, role=target_role, channel=target_channel
            ),
            text=f"You have been assigned {new_role_name} for incident <#{target_channel}>",
        )
        logger.debug(f"\n{result}\n")
    except slack_sdk.errors.SlackApiError as error:
        logger.error(f"Error sending role description to user: {error}")
    logger.info(f"{user_name} was assigned {target_role} in {channel_name}")

    # Since the user was assigned the role, they should be auto invited.
    invite_user_to_channel(target_channel, user_id)

    # Update the row to indicate who owns the role.
    db_update_incident_role(
        channel_id=target_channel, role=target_role, user=user_name
    )

    # Write audit log
    log.write(
        incident_id=channel_name,
        event=f"User {user_name} was assigned role {target_role}.",
    )
    # Finally, updated the updated_at column
    db_update_incident_updated_at_col(
        channel_id=target_channel,
        updated_at=tools.fetch_timestamp(),
    )


async def claim_role(action_parameters: type[ActionParametersSlack]):
    """When an incoming action is incident.claim_role, this method
    assigns the role to the user that hit the claim button

    Keyword arguments:
    action_parameters -- type[ActionParametersSlack] containing Slack actions data
    """
    incident_data = db_read_incident(
        channel_id=action_parameters.channel_details["id"]
    )
    action_value = action_parameters.actions["value"]
    # Find the index of the block that contains info on
    # the role we want to update
    blocks = action_parameters.message_details["blocks"]
    index = tools.find_index_in_list(
        blocks, "block_id", f"role_{action_value}"
    )
    # Replace the "_none_" value in the given block
    temp_new_role_name = action_value.replace("_", " ")
    new_role_name = temp_new_role_name.title()
    user = action_parameters.user_details["name"]
    blocks[index]["text"]["text"] = f"*{new_role_name}*:\n <@{user}>"
    # Update the message
    slack_web_client.chat_update(
        channel=incident_data.channel_id,
        ts=action_parameters.message_details["ts"],
        blocks=blocks,
    )
    # Send update notification message to incident channel
    try:
        result = slack_web_client.chat_postMessage(
            **IncidentUpdate.role(
                channel=incident_data.channel_id, role=new_role_name, user=user
            ),
            text=f"You have claimed {new_role_name} for incident <#{incident_data.channel_id}>",
        )
        logger.debug(f"\n{result}\n")
    except slack_sdk.errors.SlackApiError as error:
        logger.error(f"Error sending role update to incident channel: {error}")
    # Let the user know they've been assigned the role and what to do
    try:
        result = slack_web_client.chat_postMessage(
            **IncidentUserNotification.create(
                user=action_parameters.user_details["id"],
                role=action_value,
                channel=incident_data.channel_id,
            ),
            text=f"You have been assigned the role {action_value} for incident {incident_data.channel_name}.",
        )
        logger.debug(f"\n{result}\n")
    except slack_sdk.errors.SlackApiError as error:
        logger.error(f"Error sending role description to user: {error}")
    logger.info(
        f"{user} has claimed {action_value} in {incident_data.channel_name}"
    )
    # Update the row to indicate who owns the role.
    db_update_incident_role(
        channel_id=incident_data.channel_id, role=action_value, user=user
    )

    # Write audit log
    log.write(
        incident_id=incident_data.channel_id,
        event=f"User {user} claimed role {action_value}.",
    )
    # Finally, updated the updated_at column
    db_update_incident_updated_at_col(
        channel_id=incident_data.channel_id,
        updated_at=tools.fetch_timestamp(),
    )


async def export_chat_logs(action_parameters: type[ActionParametersSlack]):
    """When an incoming action is incident.export_chat_logs, this method
    fetches channel history, formats it, and returns it to the channel

    Keyword arguments:
    action_parameters -- type[ActionParametersSlack] containing Slack actions data
    """
    incident_data = db_read_incident(
        channel_id=action_parameters.channel_details["id"]
    )
    # Retrieve channel history and post as text attachment
    history = get_formatted_channel_history(
        channel_id=incident_data.channel_id,
        channel_name=incident_data.channel_name,
    )
    try:
        logger.info(
            f"Sending chat transcript to {incident_data.channel_name}."
        )
        result = slack_web_client.files_upload_v2(
            channels=incident_data.channel_id,
            content=history,
            filename=f"{incident_data.channel_name} Chat Transcript",
            filetype="txt",
            initial_comment="As requested, here is the chat transcript. Remember"
            + " - while this is useful, it will likely need cultivation before "
            + "being added to a postmortem.",
            title=f"{incident_data.channel_name} Chat Transcript",
        )
        logger.debug(f"\n{result}\n")
    except slack_sdk.errors.SlackApiError as error:
        logger.error(
            f"Error sending message and attachment to {incident_data.channel_name}: {error}"
        )
    finally:
        # Write audit log
        log.write(
            incident_id=incident_data.channel_name,
            event=f"Incident chat log was exported by {action_parameters.user_details}.",
        )


async def set_status(
    action_parameters: type[ActionParametersSlack] = ActionParametersSlack,
):
    """When an incoming action is incident.set_status, this method
    updates the status of the incident

    Keyword arguments:
    action_parameters(type[ActionParametersSlack]) containing Slack actions data
    """
    incident_data = db_read_incident(
        channel_id=action_parameters.channel_details["id"]
    )

    action_value = action_parameters.actions["selected_option"]["value"]
    user = action_parameters.user_details["id"]
    formatted_severity = extract_attribute(
        attribute="severity",
        channel=variables.digest_channel_id,
        oldest=incident_data.dig_message_ts,
    )

    # Write audit log
    log.write(
        incident_id=incident_data.incident_id,
        event=f"Status was changed to {action_value}.",
    )

    # If set to resolved, send additional information.
    if action_value == "resolved":
        # Set up steps for RCA channel
        message_blocks = action_parameters.message_details["blocks"]
        # Extract names of required roles
        incident_commander = extract_role_owner(
            message_blocks, "role_incident_commander"
        )
        # Error out if incident commander hasn't been claimed
        for role, person in {
            "incident commander": incident_commander,
        }.items():
            if person == "_none_":
                try:
                    result = slack_web_client.chat_postMessage(
                        channel=incident_data.channel_id,
                        text=f":red_circle: <@{user}> Before this incident can"
                        + f" be marked as resolved, the *{role}* role must be "
                        + "assigned. Please assign it and try again.",
                    )
                except slack_sdk.errors.SlackApiError as error:
                    logger.error(
                        f"Error sending note to {incident_data.incident_id} regarding missing role claim: {error}"
                    )
                return
        # Create rca channel
        rca_channel_name = f"{incident_data.incident_id}-rca"
        try:
            rca_channel = slack_web_client.conversations_create(
                name=rca_channel_name
            )
            # Log the result which includes information like the ID of the conversation
            logger.debug(f"\n{rca_channel_name}\n")
            logger.info(f"Creating rca channel: {rca_channel_name}")
            # Write audit log
            log.write(
                incident_id=incident_data.incident_id,
                event=f"RCA channel was created.",
                content=rca_channel["channel"]["id"],
            )
        except slack_sdk.errors.SlackApiError as error:
            logger.error(f"Error creating rca channel: {error}")
        # Invite incident commander and technical lead if they weren't empty
        rcaChannelDetails = {
            "id": rca_channel["channel"]["id"],
            "name": rca_channel["channel"]["name"],
        }
        # We want real user names to tag in the rca doc
        actual_user_names = []
        for person in [incident_commander]:
            if person != "_none_":
                fmt = person.replace("<", "").replace(">", "").replace("@", "")
                invite_user_to_channel(rcaChannelDetails["id"], fmt)
                # Get real name of user to be used to generate RCA
                actual_user_names.append(
                    slack_web_client.users_info(user=fmt)["user"]["profile"][
                        "real_name"
                    ]
                )
            else:
                actual_user_names.append("Unassigned")
        # Format boilerplate message to rca channel
        rca_boilerplate_message_blocks = [
            {"type": "divider"},
            {
                "type": "header",
                "text": {
                    "type": "plain_text",
                    "text": ":white_check_mark: Incident RCA Planning",
                },
            },
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": "You have been invited to this channel to assist "
                    + f"with planning the RCA for <#{incident_data.channel_id}>. The Incident Commander "
                    + "should invite anyone who can help contribute to the RCA"
                    + " and then use this channel to plan the meeting to go over the incident.",
                },
            },
        ]
        # Generate rca template and create rca if enabled
        # Get normalized description as rca title
        if "confluence" in config.active.integrations.get(
            "atlassian"
        ) and config.active.integrations.get("atlassian").get(
            "confluence"
        ).get(
            "auto_create_rca"
        ):
            from bot.confluence.rca import IncidentRootCauseAnalysis

            rca_title = " ".join(incident_data.incident_id.split("-")[2:])
            rca = IncidentRootCauseAnalysis(
                incident_id=incident_data.incident_id,
                rca_title=rca_title,
                incident_commander=actual_user_names[0],
                severity=formatted_severity,
                severity_definition=config.active.severities[
                    formatted_severity
                ],
                pinned_items=read_incident_pinned_items(
                    incident_id=incident_data.incident_id
                ),
                timeline=log.read(incident_id=incident_data.incident_id),
            )
            rca_link = rca.create()
            db_update_incident_rca_col(
                channel_id=incident_data.channel_id,
                rca=rca_link,
            )
            # Write audit log
            log.write(
                incident_id=incident_data.incident_id,
                event=f"RCA was automatically created: {rca_link}",
            ),
            rca_boilerplate_message_blocks.extend(
                [
                    {
                        "type": "section",
                        "text": {
                            "type": "mrkdwn",
                            "text": "*I have created a base RCA document that"
                            " you can build on. You can open it using the button below.*",
                        },
                    },
                    {
                        "block_id": "buttons",
                        "type": "actions",
                        "elements": [
                            {
                                "type": "button",
                                "text": {
                                    "type": "plain_text",
                                    "text": "View RCA In Confluence",
                                },
                                "style": "primary",
                                "url": rca_link,
                                "action_id": "open_rca",
                            },
                            {
                                "type": "button",
                                "text": {
                                    "type": "plain_text",
                                    "text": "View Incident Channel",
                                },
                                "url": f"https://{slack_workspace_id}.slack.com/archives/{incident_data.channel_id}",
                                "action_id": "incident.join_incident_channel",
                            },
                        ],
                    },
                    {"type": "divider"},
                ]
            )
        else:
            rca_boilerplate_message_blocks.extend(
                [
                    {
                        "block_id": "buttons",
                        "type": "actions",
                        "elements": [
                            {
                                "type": "button",
                                "text": {
                                    "type": "plain_text",
                                    "text": "View Incident Channel",
                                },
                                "url": f"https://{slack_workspace_id}.slack.com/archives/{incident_data.channel_id}",
                                "action_id": "incident.join_incident_channel",
                            },
                        ],
                    },
                    {"type": "divider"},
                ]
            )
        try:
            blocks = rca_boilerplate_message_blocks
            result = slack_web_client.chat_postMessage(
                channel=rcaChannelDetails["id"],
                blocks=blocks,
                text="",
            )
            logger.debug(f"\n{result}\n")

        except slack_sdk.errors.SlackApiError as error:
            logger.error(f"Error sending RCA update to RCA channel: {error}")

        # Send message to incident channel
        try:
            result = slack_web_client.chat_postMessage(
                **IncidentResolutionMessage.create(
                    channel=incident_data.channel_id
                ),
                text="The incident has been resolved.",
            )
            logger.debug(f"\n{result}\n")
        except slack_sdk.errors.SlackApiError as error:
            logger.error(
                f"Error sending resolution update to incident channel {incident_data.channel_name}: {error}"
            )

        # Log
        logger.info(f"Sent resolution info to {incident_data.channel_name}.")

        # If PagerDuty incident(s) exist, attempt to resolve them
        if "pagerduty" in config.active.integrations:
            from bot.pagerduty.api import resolve

            if incident_data.pagerduty_incidents is not None:
                for inc in incident_data.pagerduty_incidents:
                    resolve(pd_incident_id=inc)

    # Also updates digest message
    try:
        slack_web_client.chat_update(
            channel=variables.digest_channel_id,
            ts=incident_data.dig_message_ts,
            blocks=IncidentChannelDigestNotification.update(
                incident_id=incident_data.channel_name,
                incident_description=incident_data.channel_description,
                is_security_incident=incident_data.is_security_incident,
                status=action_value,
                severity=formatted_severity,
                conference_bridge=incident_data.conference_bridge,
            ),
            text="",
        )
    except slack_sdk.errors.SlackApiError as e:
        logger.error(
            f"Error sending status update to incident channel {incident_data.channel_name}: {error}"
        )

    # Change placeholder for select to match current status in boilerplate message
    result = slack_web_client.conversations_history(
        channel=incident_data.channel_id,
        inclusive=True,
        oldest=incident_data.bp_message_ts,
        limit=1,
    )
    blocks = result["messages"][0]["blocks"]
    status_block_index = tools.find_index_in_list(blocks, "block_id", "status")
    blocks[status_block_index]["accessory"]["initial_option"] = {
        "text": {
            "type": "plain_text",
            "text": action_value.title(),
            "emoji": True,
        },
        "value": action_value,
    }
    slack_web_client.chat_update(
        channel=incident_data.channel_id,
        ts=action_parameters.message_details["ts"],
        blocks=blocks,
    )

    # Update incident record with the status
    logger.info(
        f"Updating incident record in database with new status for {incident_data.channel_name}"
    )
    try:
        db_update_incident_status_col(
            channel_id=incident_data.channel_id,
            status=action_value,
        )
    except Exception as error:
        logger.fatal(f"Error updating entry in database: {error}")

    # See if there's a scheduled reminder job for the incident and delete it if so
    if action_value == "resolved":
        for job in scheduler.process.list_jobs():
            job_title = f"{incident_data.channel_name}_updates_reminder"
            if job.id == job_title:
                try:
                    scheduler.process.delete_job(job_title)
                    logger.info(f"Deleted job: {job_title}")
                    # Write audit log
                    log.write(
                        incident_id=incident_data.channel_name,
                        event="Deleted scheduled reminder for incident updates.",
                    )
                except Exception as error:
                    logger.error(
                        f"Could not delete the job {job_title}: {error}"
                    )

    # If the incident is resolved, disable status select
    if action_value == "resolved":
        result = slack_web_client.conversations_history(
            channel=incident_data.channel_id,
            inclusive=True,
            oldest=incident_data.bp_message_ts,
            limit=1,
        )
        blocks = result["messages"][0]["blocks"]
        status_block_index = tools.find_index_in_list(
            blocks, "block_id", "status"
        )
        blocks[status_block_index]["accessory"]["confirm"] = {
            "title": {
                "type": "plain_text",
                "text": "This incident is already resolved.",
            },
            "text": {
                "type": "mrkdwn",
                "text": "Since this incident has already been resolved, it "
                + "shouldn't be reopened. A new incident should be started instead.",
            },
            "confirm": {"type": "plain_text", "text": "Reopen Anyway"},
            "deny": {"type": "plain_text", "text": "Go Back"},
            "style": "danger",
        }
        slack_web_client.chat_update(
            channel=incident_data.channel_id,
            ts=action_parameters.message_details["ts"],
            blocks=blocks,
        )
    # Log
    logger.info(
        f"Updated incident status for {incident_data.channel_name} to {action_value}."
    )
    try:
        result = slack_web_client.chat_postMessage(
            **IncidentUpdate.status(
                channel=incident_data.channel_id, status=action_value
            ),
            text=f"The incident status has been changed to {action_value}.",
        )
        logger.debug(f"\n{result}\n")
    except slack_sdk.errors.SlackApiError as error:
        logger.error(
            f"Error sending status update to incident channel {incident_data.channel_name}: {error}"
        )
    # Finally, updated the updated_at column
    db_update_incident_updated_at_col(
        channel_id=incident_data.channel_id,
        updated_at=tools.fetch_timestamp(),
    )


async def set_severity(
    action_parameters: type[ActionParametersSlack] = None,
):
    """When an incoming action is incident.set_severity, this method
    updates the severity of the incident

    Keyword arguments:
    action_parameters(type[ActionParametersSlack]) - contains Slack actions data
    """
    incident_data = db_read_incident(
        channel_id=action_parameters.channel_details["id"]
    )
    action_value = action_parameters.actions["selected_option"]["value"]

    # Also updates digest message
    # Retrieve the existing value of status since we need to put that back
    formatted_status = extract_attribute(
        attribute="status",
        channel=variables.digest_channel_id,
        oldest=incident_data.dig_message_ts,
    )
    try:
        slack_web_client.chat_update(
            channel=variables.digest_channel_id,
            ts=incident_data.dig_message_ts,
            blocks=IncidentChannelDigestNotification.update(
                incident_id=incident_data.channel_name,
                incident_description=incident_data.channel_description,
                is_security_incident=incident_data.is_security_incident,
                status=formatted_status,
                severity=action_value,
                conference_bridge=incident_data.conference_bridge,
            ),
        )
    except slack_sdk.errors.SlackApiError as error:
        logger.error(
            f"Error sending severity update to incident channel {incident_data.channel_name}: {error}"
        )

    # Change placeholder for select to match current status in boilerplate message
    result = slack_web_client.conversations_history(
        channel=incident_data.channel_id,
        inclusive=True,
        oldest=incident_data.bp_message_ts,
        limit=1,
    )
    blocks = result["messages"][0]["blocks"]
    sev_blocks_index = tools.find_index_in_list(blocks, "block_id", "severity")
    blocks[sev_blocks_index]["accessory"]["initial_option"] = {
        "text": {
            "type": "plain_text",
            "text": action_value.upper(),
            "emoji": True,
        },
        "value": action_value,
    }
    slack_web_client.chat_update(
        channel=incident_data.channel_id,
        ts=action_parameters.message_details["ts"],
        blocks=blocks,
    )

    # Update incident record with the severity
    logger.info(
        f"Updating incident record in database with new severity for {incident_data.channel_name}"
    )
    try:
        db_update_incident_severity_col(
            channel_id=incident_data.channel_id,
            severity=action_value,
        )
    except Exception as error:
        logger.fatal(f"Error updating entry in database: {error}")

    # If SEV1/2, we need to start a timer to remind the channel about sending status updates
    if action_value in ["sev1", "sev2"]:
        logger.info(f"Adding job because action was {action_value}")
        scheduler.add_incident_scheduled_reminder(
            channel_name=incident_data.channel_name,
            channel_id=incident_data.channel_id,
            severity=action_value,
        )
        # Write audit log
        log.write(
            incident_id=incident_data.channel_name,
            event=f"Scheduled reminder job created.",
        )

    # Final notification
    try:
        result = slack_web_client.chat_postMessage(
            **IncidentUpdate.severity(
                channel=incident_data.channel_id, severity=action_value
            ),
            text=f"The incident severity has been changed to {action_value}.",
        )
        logger.debug(f"\n{result}\n")
    except slack_sdk.errors.SlackApiError as error:
        logger.error(
            f"Error sending severity update to incident channel {incident_data.channel_name}: {error}"
        )
    # Log
    logger.info(
        f"Updated incident severity for {incident_data.channel_name} to {action_value}."
    )
    # Finally, updated the updated_at column
    db_update_incident_updated_at_col(
        channel_id=incident_data.channel_id,
        updated_at=tools.fetch_timestamp(),
    )
    # Write audit log
    log.write(
        incident_id=incident_data.channel_name,
        event=f"Severity set to {action_value.upper()}.",
    )


"""
Utility Functions
"""


def extract_role_owner(message_blocks: Dict[Any, Any], block_id: str) -> str:
    """
    Takes message blocks and a block_id and returns information specific
    to one of the role blocks
    """
    index = tools.find_index_in_list(message_blocks, "block_id", block_id)
    return (
        message_blocks[index]["text"]["text"].split("\n")[1].replace(" ", "")
    )


def extract_attribute(
    attribute: str,
    channel: str,
    oldest: Any,
) -> str:
    """
    References existing data in the digest message
    """
    try:
        result = slack_web_client.conversations_history(
            channel=channel,
            inclusive=True,
            oldest=oldest,
            limit=1,
        )
        message = result["messages"][0]
        index = tools.find_index_in_list(
            message["blocks"], "block_id", f"digest_channel_{attribute}"
        )
        current = message["blocks"][index]["text"]["text"]
        regex = "\*(.*?)\*"
        return re.search(regex, current).group(1).replace("*", "").lower()
    except slack_sdk.errors.SlackApiError as error:
        logger.error(
            f"Error retrieving current {attribute} from digest message: {error}"
        )
