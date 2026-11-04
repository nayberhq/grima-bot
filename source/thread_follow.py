"""Keep signup actions and membership of the clicked event thread in sync."""
import logging
import discord

logger = logging.getLogger(__name__)


async def follow_signup(interaction, following):
    # Ordinary channel posts have no thread membership to change.
    thread = interaction.channel
    if not isinstance(thread, discord.Thread):
        return
    if not following:
        return
    try:
        mention = await thread.send(
            f'<@{interaction.user.id}>',
            allowed_mentions=discord.AllowedMentions(users=[interaction.user]),
            silent=True,
        )
        await mention.delete()
    except discord.HTTPException:
        logger.warning("Could not follow event thread %s for user %s",
                       thread.id, interaction.user.id, exc_info=True)
        try:
            await interaction.followup.send(
                'Your signup change was saved, but I could not follow this post. '
                'Please follow it manually; a raid leader can check my thread permissions.',
                ephemeral=True)
        except discord.HTTPException:
            logger.warning("Could not deliver thread membership warning", exc_info=True)


async def unfollow_all(thread, *, bot=None):
    """Remove every member from an event thread when the event is deleted."""
    if not isinstance(thread, discord.Thread):
        return
    try:
        members = await thread.fetch_members()
    except discord.HTTPException:
        logger.warning('Could not list members for thread %s', thread.id, exc_info=True)
        return
    bot_id = bot.id if bot else None
    for member in members:
        if bot_id and member.id == bot_id:
            continue
        try:
            await thread.remove_user(member)
        except discord.HTTPException:
            logger.warning('Could not unfollow user %s from thread %s', member.id, thread.id, exc_info=True)
