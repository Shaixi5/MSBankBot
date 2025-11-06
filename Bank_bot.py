# bank_ticket_bot.py
# Python 3.10+ | discord.py 2.4+
# pip install -U discord.py python-dotenv

import os
import io
import re
import datetime as dt
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv
from flask import Flask
app = Flask(__name__)

@app.route('/')
def home():
    return "Bot is running!"




load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")
GUILD_ID = int(os.getenv("GUILD_ID", "0"))  # 0 = global commands (propagation delay). Set your server ID for instant sync.
# Tickets category: set an ID or a name (the bot will find/create by name if ID is 0)
TICKETS_CATEGORY_ID = int(os.getenv("TICKETS_CATEGORY_ID", "0"))
TICKETS_CATEGORY_NAME = os.getenv("TICKETS_CATEGORY_NAME", "📥 bank-tickets")
# Approvers: role IDs and (optional) specific user IDs
APPROVER_ROLE_IDS = [int(x) for x in os.getenv("APPROVER_ROLE_IDS", "").split(",") if x.strip()]
APPROVER_USER_IDS = [int(x) for x in os.getenv("APPROVER_USER_IDS", "").split(",") if x.strip()]
# Optional logs channel for transcripts on close
LOG_CHANNEL_ID = int(os.getenv("LOG_CHANNEL_ID", "0"))

INTENTS = discord.Intents.default()
INTENTS.members = True  # needed to check roles for approvers
bot = commands.Bot(command_prefix="!", intents=INTENTS)

# ------------------------------ Helpers ------------------------------

def parse_amount(text: str) -> int:
    """
    Parse amounts like '12m', '10k', '1.5b', '1200000', '1,200,000' → int.
    Supports suffixes: k (1e3), m (1e6), b (1e9).
    """
    if text is None:
        raise ValueError("no amount provided")
    s = text.strip().lower().replace(",", "").replace(" ", "")
    m = re.fullmatch(r"([0-9]*\.?[0-9]+)([kmb]?)", s)
    if not m:
        raise ValueError("invalid amount format")
    num = float(m.group(1))
    suf = m.group(2)
    mult = 1
    if suf == "k":
        mult = 1_000
    elif suf == "m":
        mult = 1_000_000
    elif suf == "b":
        mult = 1_000_000_000
    value = int(num * mult)
    if value <= 0:
        raise ValueError("amount must be > 0")
    return value

def is_approver(member: discord.Member) -> bool:
    if member.guild_permissions.administrator:
        return True
    if member.id in APPROVER_USER_IDS:
        return True
    approver_roles = set(APPROVER_ROLE_IDS)
    return any(r.id in approver_roles for r in member.roles)

async def make_transcript(channel: discord.TextChannel) -> discord.File:
    """Create a simple text transcript of the channel's history and return as a File."""
    lines = []
    async for msg in channel.history(limit=None, oldest_first=True):
        ts = msg.created_at.strftime("%Y-%m-%d %H:%M:%S UTC")
        author = f"{msg.author} [{msg.author.id}]"
        content = msg.content.replace("\n", "\n    ")
        lines.append(f"[{ts}] {author}:\n    {content}")
        for a in msg.attachments:
            lines.append(f"    [attachment] {a.filename} -> {a.url}")
    buff = io.StringIO("\n".join(lines) or "(no messages)")
    fname = f"transcript_{channel.name}_{int(dt.datetime.utcnow().timestamp())}.txt"
    return discord.File(fp=buff, filename=fname)

async def resolve_or_create_tickets_category(guild: discord.Guild) -> Optional[discord.CategoryChannel]:
    category: Optional[discord.CategoryChannel] = None
    if TICKETS_CATEGORY_ID:
        ch = guild.get_channel(TICKETS_CATEGORY_ID)
        if isinstance(ch, discord.CategoryChannel):
            category = ch
    if category is None:
        for c in guild.categories:
            if c.name.lower() == TICKETS_CATEGORY_NAME.lower():
                category = c
                break
    if category is None:
        category = await guild.create_category(TICKETS_CATEGORY_NAME, reason="Create tickets category")
    return category

async def lock_channel(channel: discord.TextChannel):
    """Remove send permissions from everyone; keep visible to approvers for record."""
    overwrites = channel.overwrites
    for target, ow in list(overwrites.items()):
        if isinstance(target, (discord.Role, discord.Member)):
            ow.send_messages = False
            overwrites[target] = ow
    await channel.edit(overwrites=overwrites)

# ------------------------------ UI: Modal & Views ------------------------------

class BankRequestModal(discord.ui.Modal, title="Faction Bank Request"):
    amount = discord.ui.TextInput(
        label="Amount",
        placeholder="e.g., 12m or 10k or 1,200,000",
        required=True,
        max_length=30
    )
    comment = discord.ui.TextInput(
        label="Comment (optional)",
        style=discord.TextStyle.paragraph,
        placeholder="Extra details if needed",
        required=False,
        max_length=1000
    )

    def __init__(self, author: discord.Member):
        super().__init__()
        self.author = author

    async def on_submit(self, interaction: discord.Interaction):
        guild = interaction.guild
        if guild is None:
            return await interaction.response.send_message("Use this in a server.", ephemeral=True)
        # parse flexible amount
        try:
            amt = parse_amount(str(self.amount.value))
        except Exception:
            return await interaction.response.send_message(
                "Amount must be like `1200000`, `1,200,000`, `10k`, `12m`, or `1.5b`.",
                ephemeral=True
            )
        # Ask the user to choose WHEN to send (required)
        await interaction.response.send_message(
            content="Select **when** to send the funds:",
            view=OptionSelectView(requester=self.author, amount=amt, comment=self.comment.value or ""),
            ephemeral=True
        )

class OptionSelect(discord.ui.Select):
    def __init__(self, parent_view: "OptionSelectView"):
        opts = [
            discord.SelectOption(label="ASAP", value="ASAP", description="Send as soon as approved"),
            discord.SelectOption(label="Only if I am online", value="online"),
            discord.SelectOption(label="Only if I am in Hospital", value="hospital"),
            discord.SelectOption(label="Only if I am Flying", value="flying"),
        ]
        super().__init__(placeholder="Choose one option…", min_values=1, max_values=1, options=opts, custom_id="bank_option_select")
        self.parent_view = parent_view

    async def callback(self, interaction: discord.Interaction):
        view = self.parent_view
        guild = interaction.guild
        if guild is None:
            return await interaction.response.send_message("Use this in a server.", ephemeral=True)
        if interaction.user.id != view.requester.id:
            return await interaction.response.send_message("This selection isn't for you.", ephemeral=True)

        chosen = self.values[0]
        # Resolve/create category
        category = await resolve_or_create_tickets_category(guild)
        if category is None:
            return await interaction.response.send_message("Couldn't resolve or create a tickets category.", ephemeral=True)

        overwrites = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            view.requester: discord.PermissionOverwrite(view_channel=True, send_messages=True, attach_files=True,
                                                        read_message_history=True, add_reactions=False),
            guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True, manage_channels=True,
                                                  read_message_history=True),
        }
        for rid in APPROVER_ROLE_IDS:
            role = guild.get_role(rid)
            if role:
                overwrites[role] = discord.PermissionOverwrite(
                    view_channel=True, send_messages=True, read_message_history=True,
                    manage_messages=True, attach_files=True
                )

        safe_name = view.requester.name.lower().replace(" ", "-")
        ch_name = f"bank-{safe_name}-{view.requester.id}"[:95]
        channel = await category.create_text_channel(name=ch_name, overwrites=overwrites, reason="Bank request ticket")

        mapping = {
            "ASAP": "ASAP",
            "online": "Only if I am online",
            "hospital": "Only if I am in Hospital",
            "flying": "Only if I am Flying",
        }

        embed = discord.Embed(title="💸 Faction Bank Request", color=discord.Color.blurple(), timestamp=dt.datetime.utcnow())
        embed.add_field(name="Member", value=f"{view.requester.mention} ({view.requester.id})", inline=False)
        embed.add_field(name="Amount", value=f"{view.amount:,}", inline=True)
        embed.add_field(name="When to send", value=mapping[chosen], inline=True)
        if view.comment:
            embed.add_field(name="Comment", value=view.comment[:1000], inline=False)
        embed.set_footer(text=f"Ticket ID: {channel.id}")

        await channel.send(
            content=", ".join([r.mention for r in [guild.get_role(rid) for rid in APPROVER_ROLE_IDS] if r]),
            embed=embed,
            view=ApprovalView(requester_id=view.requester.id)
        )



        # NEW: DM all members with the approver roles (e.g. Bank Manager)
        for rid in APPROVER_ROLE_IDS:
            role = guild.get_role(rid)
            if role:
                for member in role.members:
                    try:
                        await member.send(
                            f"💸 New bank request from {view.requester.mention}\n"
                            f"Amount: **{view.amount:,}**\n"
                            f"When: **{mapping[chosen]}**\n"
                            f"Ticket channel: {channel.mention}"
                        )
                    except discord.Forbidden:
                        # They have DMs closed or blocked the bot
                        pass

        
        await interaction.response.edit_message(
            content=f"Option selected: **{mapping[chosen]}** — ticket created: {channel.mention}",
            view=None
        )

class OptionSelectView(discord.ui.View):
    def __init__(self, requester: discord.Member, amount: int, comment: str):
        super().__init__(timeout=300)
        self.requester = requester
        self.amount = amount
        self.comment = comment
        self.add_item(OptionSelect(self))

class ApprovalView(discord.ui.View):
    def __init__(self, requester_id: int, *, timeout: Optional[float] = 7 * 24 * 3600):
        super().__init__(timeout=timeout)
        self.requester_id = requester_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("Only members can use this.", ephemeral=True)
            return False
        if not is_approver(interaction.user):
            await interaction.response.send_message("You don't have permission to do that.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Approve", style=discord.ButtonStyle.success)
    async def approve(self, interaction: discord.Interaction, _: discord.ui.Button):
        embed = interaction.message.embeds[0] if interaction.message.embeds else None
        if embed:
            embed.color = discord.Color.green()
            embed.add_field(name="Status", value=f"✅ Approved by {interaction.user.mention}")
        await interaction.response.edit_message(content="", embed=embed, view=self)
        await interaction.followup.send("Request approved. Send funds, then hit **Close** when done.")

    @discord.ui.button(label="Reject", style=discord.ButtonStyle.danger)
    async def reject(self, interaction: discord.Interaction, _: discord.ui.Button):
        embed = interaction.message.embeds[0] if interaction.message.embeds else None
        if embed:
            embed.color = discord.Color.red()
            embed.add_field(name="Status", value=f"❌ Rejected by {interaction.user.mention}")
        await interaction.response.edit_message(content="", embed=embed, view=self)

    @discord.ui.button(label="Close", style=discord.ButtonStyle.secondary)
    async def close(self, interaction: discord.Interaction, _: discord.ui.Button):
        channel = interaction.channel
        if not isinstance(channel, discord.TextChannel):
            return await interaction.response.send_message("Not a text channel.", ephemeral=True)

        # Make transcript
        file = await make_transcript(channel)

        # Notify + upload transcript
        await interaction.response.send_message("Closing… uploading transcript and deleting this channel.", ephemeral=True)
        logged = False
        if interaction.guild and LOG_CHANNEL_ID:
            log_ch = interaction.guild.get_channel(LOG_CHANNEL_ID)
            if isinstance(log_ch, discord.TextChannel):
                try:
                    await log_ch.send(
                        content=f"📄 Transcript for **{channel.name}** (closed by {interaction.user.mention})",
                        file=file
                    )
                    logged = True
                except Exception:
                    logged = False
        if not logged:
            try:
                await interaction.user.send(content=f"📄 Transcript for **{channel.name}**", file=file)
            except Exception:
                pass

        # Delete ticket channel
        try:
            await channel.delete(reason=f"Closed by {interaction.user}")
        except Exception:
            # Fallback: lock & rename if deletion fails
            await lock_channel(channel)
            await channel.edit(name=f"closed-{channel.name}")

class OpenTicketView(discord.ui.View):
    """Persistent panel with a button users click to open the request modal."""
    def __init__(self):
        super().__init__(timeout=None)  # persistent across restarts

    @discord.ui.button(label="Open Bank Ticket", style=discord.ButtonStyle.primary, custom_id="open_bank_ticket_v2")
    async def open_ticket(self, interaction: discord.Interaction, _: discord.ui.Button):
        await interaction.response.send_modal(BankRequestModal(author=interaction.user))

# ------------------------------ Cog (slash commands) ------------------------------

class BankRequest(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="panel", description="Post the bank request panel with the 'Open Bank Ticket' button.")
    async def panel(self, interaction: discord.Interaction):
        if not isinstance(interaction.user, discord.Member) or not is_approver(interaction.user):
            return await interaction.response.send_message("You don't have permission to place the panel.", ephemeral=True)
        embed = discord.Embed(
            title="💸 Faction Bank Requests",
            description=(
                "Click **Open Bank Ticket** to privately request money from the faction bank.\n\n"
                "**Amount** supports 10k / 12m / 1.5b or comma’d numbers.\n"
                "**Comment** is optional. After submitting, you **must choose one** of: "
                "ASAP, Only if I am online, Only if I am in Hospital, Only if I am Flying."
            ),
            color=discord.Color.blurple()
        )
        await interaction.channel.send(embed=embed, view=OpenTicketView())
        await interaction.response.send_message("Panel posted.", ephemeral=True)

    @app_commands.command(name="bankrequest", description="Open a private ticket to request money from the faction bank.")
    async def bankrequest(self, interaction: discord.Interaction):
        await interaction.response.send_modal(BankRequestModal(author=interaction.user))

    @app_commands.command(name="close", description="Close the current ticket (approvers only).")
    async def close(self, interaction: discord.Interaction):
        if not isinstance(interaction.user, discord.Member) or not is_approver(interaction.user):
            return await interaction.response.send_message("You don't have permission to close this.", ephemeral=True)
        channel = interaction.channel
        if not isinstance(channel, discord.TextChannel):
            return await interaction.response.send_message("Not a text channel.", ephemeral=True)

        # Make transcript
        file = await make_transcript(channel)

        # Notify + upload transcript
        await interaction.response.send_message("Closing… uploading transcript and deleting this channel.", ephemeral=True)
        logged = False
        if interaction.guild and LOG_CHANNEL_ID:
            log_ch = interaction.guild.get_channel(LOG_CHANNEL_ID)
            if isinstance(log_ch, discord.TextChannel):
                try:
                    await log_ch.send(
                        content=f"📄 Transcript for **{channel.name}** (closed by {interaction.user.mention})",
                        file=file
                    )
                    logged = True
                except Exception:
                    logged = False
        if not logged:
            try:
                await interaction.user.send(content=f"📄 Transcript for **{channel.name}**", file=file)
            except Exception:
                pass

        # Delete ticket channel
        try:
            await channel.delete(reason=f"Closed by {interaction.user}")
        except Exception:
            await lock_channel(channel)
            await channel.edit(name=f"closed-{channel.name}")

    @app_commands.command(name="ping", description="Check if the bot is alive and commands are synced.")
    async def ping(self, interaction: discord.Interaction):
        await interaction.response.send_message("Pong! ✅", ephemeral=True)

    @app_commands.command(name="sync", description="Approver/admin: force-resync slash commands to this server.")
    async def sync(self, interaction: discord.Interaction):
        if not isinstance(interaction.user, discord.Member) or not is_approver(interaction.user):
            return await interaction.response.send_message("You don't have permission to sync.", ephemeral=True)
        try:
            await self.bot.tree.sync(guild=interaction.guild)
            await interaction.response.send_message("Slash commands force-synced to this server.", ephemeral=True)
        except Exception as e:
            await interaction.response.send_message(f"Sync failed: {e}", ephemeral=True)

# ------------------------------ Startup ------------------------------

if not TOKEN:
    raise SystemExit("Missing DISCORD_TOKEN in environment.")

@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} (id={bot.user.id})")

@bot.event
async def setup_hook():
    # Register cog and persistent views AFTER all classes are defined.
    await bot.add_cog(BankRequest(bot))
    bot.add_view(OpenTicketView())

    # Sync commands
    try:
        if GUILD_ID:
            guild = bot.get_guild(GUILD_ID)
            if guild:
                bot.tree.copy_global_to(guild=guild)
                await bot.tree.sync(guild=guild)
                print(f"Slash commands synced to guild {guild.name} ({guild.id}) — instant availability.")
            else:
                await bot.tree.sync()
                print("Guild not found; synced globally (may take a while to propagate).")
        else:
            await bot.tree.sync()
            print("Slash commands synced globally (may take a while to propagate).")
    except Exception as e:
        print("Sync error:", e)

import threading

def run_web():
    app.run(host="0.0.0.0", port=10000)

threading.Thread(target=run_web).start()


bot.run(TOKEN)
