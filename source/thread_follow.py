"""Keep signup actions and membership of the clicked event thread in sync."""
import logging
import discord

logger = logging.getLogger(__name__)


async def follow_signup(interaction, following):
    # Ordinary channel posts have no thread membership to change.
    thread = interaction.channel
    if not isinstance(thread, discord.Thread):
        return
    try:
        if following:
            await thread.add_user(interaction.user)
        else:
            await thread.remove_user(interaction.user)
    except discord.HTTPException:
        action = 'follow' if following else 'unfollow'
        logger.warning("Could not %s event thread %s for user %s",
                       action, thread.id, interaction.user.id, exc_info=True)
        try:
            await interaction.followup.send(
                f'Your signup change was saved, but I could not {action} this post. '
                f'Please {action} it manually; a raid leader can check my thread permissions.',
                ephemeral=True)
        except discord.HTTPException:
            logger.warning("Could not deliver thread membership warning", exc_info=True)
