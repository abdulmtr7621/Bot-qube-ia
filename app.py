"""
Enhanced Discord bot with AI-powered command creation and moderation.

This is a cleaned, safer, and more robust single-file version of your bot.
Fixes / improvements included:
- More robust AI command parsing (extracts code blocks with regex and falls back to heuristics)
- Better error handling and helpful messages in /describe_command
- Defensive JSONBin reads/writes and retries
- Completed command handlers (no truncated functions)
- Slightly tightened AST-based validation and clearer error responses

Before running:
1. Create a .env with DISCORD_TOKEN, JSONBIN_MASTER_KEY, ROOT_BIN_ID. OPENAI_API_KEY is optional but required for AI features.
2. Install dependencies (example):
   pip install -U discord.py aiohttp python-dotenv openai

Run with: python qubeia_fixed.py
"""

import os
import sys
import logging
import asyncio
import inspect
import ast
import textwrap
import traceback
import re
from typing import Dict, Any, Optional

from dotenv import load_dotenv
load_dotenv()

import discord
from discord.ext import commands
from discord import app_commands
import aiohttp
import json
import openai

# --- Configuration ---
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
JSONBIN_MASTER_KEY = os.getenv("JSONBIN_MASTER_KEY")
ROOT_BIN_ID = os.getenv("ROOT_BIN_ID")

if not all([DISCORD_TOKEN, JSONBIN_MASTER_KEY, ROOT_BIN_ID]):
    print("ERROR: Missing configuration. Require: DISCORD_TOKEN, JSONBIN_MASTER_KEY, ROOT_BIN_ID")
    sys.exit(1)

JSONBIN_BASE = "https://api.jsonbin.io/v3/b"

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("bot")

intents = discord.Intents.default()
intents.members = True
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

# Caches
dynamic_commands_cache: Dict[str, Dict[str, Any]] = {}
guild_cache: Dict[str, Dict[str, Any]] = {}

# Emojis (keep as-is from your original)
EMOJI_LOCK = "<:lock_1430539488932790314:1433787684228432003>"
EMOJI_CODE = "<:code_1430539182584889466:1433787671813034025>"
EMOJI_THINK = "<:blue_link_1430538832884793459:1433787661709082634>"
EMOJI_BIN = "<:bin_1430538765805555712:1433787658810953762>"
EMOJI_BELL = "<:bell_1430538717931765852:1433787654453067786>"
EMOJI_CLOCK = "<:clock_1430539143276138630:1433787669917470830>"
EMOJI_HAMMER = "<:hammer_1430539346783764571:1433787678905602172>"
EMOJI_GAME = "<:game_1430539280522022984:1433787676988801191>"
EMOJI_APPROVED = "<:Approved:1429498035217371287>"
EMOJI_DENIED = "<:Denied:1429498036903477248>"
EMOJI_MESSAGE = "<:message:1429116387560915067>"
EMOJI_BIN2 = "<:book_1430539008894570498:1433787666033279016>"
EMOJI_EXCL = "<:exclamation_mark_143053923649873:1433787673570443306>"
EMOJI_CHECK = "<:checksecurity:1429488510871802018>"

# ---------------- JSONBin helpers ----------------
async def _jsonbin_get(session: aiohttp.ClientSession, bin_id: str, master_key: str) -> Dict[str, Any]:
    url = f"{JSONBIN_BASE}/{bin_id}/latest"
    headers = {"X-Master-Key": master_key}
    for attempt in range(3):
        try:
            async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return data.get("record", {}) or {}
                else:
                    log.warning("JSONBin GET attempt %s failed: %s", attempt + 1, resp.status)
        except Exception as e:
            log.exception("JSONBin GET exception on attempt %s: %s", attempt + 1, e)
        await asyncio.sleep(1)
    log.error("JSONBin GET failed after retries for %s", bin_id)
    return {}

async def _jsonbin_put(session: aiohttp.ClientSession, bin_id: str, master_key: str, record: Dict[str, Any]) -> bool:
    url = f"{JSONBIN_BASE}/{bin_id}"
    headers = {"X-Master-Key": master_key, "Content-Type": "application/json"}
    payload = json.dumps(record)
    for attempt in range(3):
        try:
            async with session.put(url, headers=headers, data=payload, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                if resp.status in (200, 201):
                    return True
                else:
                    log.warning("JSONBin PUT attempt %s failed: %s", attempt + 1, resp.status)
        except Exception as e:
            log.exception("JSONBin PUT exception on attempt %s: %s", attempt + 1, e)
        await asyncio.sleep(1)
    log.error("JSONBin PUT failed after retries for %s", bin_id)
    return False

async def get_root_record() -> Dict[str, Any]:
    async with aiohttp.ClientSession() as session:
        return await _jsonbin_get(session, ROOT_BIN_ID, JSONBIN_MASTER_KEY)

async def save_root_record(record: Dict[str, Any]) -> bool:
    async with aiohttp.ClientSession() as session:
        return await _jsonbin_put(session, ROOT_BIN_ID, JSONBIN_MASTER_KEY, record)

async def load_all_from_bin():
    """Load guild bin configurations and cache data from each guild's bin."""
    global dynamic_commands_cache, guild_cache
    dynamic_commands_cache = {}
    guild_cache = {}

    record = await get_root_record()
    guild_configs = record.get("guild_bin_configs") or {}

    for guild_id in guild_configs.keys():
        guild_data = await load_guild_data(guild_id)
        guild_cache[guild_id] = guild_data

        if "dynamic_commands" in guild_data:
            dynamic_commands_cache[guild_id] = guild_data["dynamic_commands"]

    log.info("Loaded %d guilds from their own JSONBins", len(guild_cache))

async def get_guild_bin_config(guild_id: str) -> Optional[Dict[str, str]]:
    record = await get_root_record()
    guild_configs = record.get("guild_bin_configs") or {}
    return guild_configs.get(guild_id)

async def save_guild_bin_config(guild_id: str, bin_id: str, master_key: str) -> bool:
    record = await get_root_record()
    if "guild_bin_configs" not in record:
        record["guild_bin_configs"] = {}
    record["guild_bin_configs"][guild_id] = {
        "bin_id": bin_id,
        "master_key": master_key,
    }
    return await save_root_record(record)

async def load_guild_data(guild_id: str) -> Dict[str, Any]:
    if guild_id in guild_cache:
        return guild_cache[guild_id] or {}

    config = await get_guild_bin_config(guild_id)
    if not config:
        log.debug("No JSONBin config for guild %s", guild_id)
        guild_cache[guild_id] = {}
        return {}

    async with aiohttp.ClientSession() as session:
        data = await _jsonbin_get(session, config["bin_id"], config["master_key"])
        guild_cache[guild_id] = data or {}
        return guild_cache[guild_id]

async def save_guild_data(guild_id: str, guild_data: Dict[str, Any]) -> bool:
    guild_cache[guild_id] = guild_data

    config = await get_guild_bin_config(guild_id)
    if not config:
        log.error("No JSONBin config for guild %s", guild_id)
        return False

    async with aiohttp.ClientSession() as session:
        return await _jsonbin_put(session, config["bin_id"], config["master_key"], guild_data)

async def save_dynamic_command(guild_id: str, cmd_name: str, code: str, description: str = None) -> bool:
    if guild_id not in dynamic_commands_cache:
        dynamic_commands_cache[guild_id] = {}
    dynamic_commands_cache[guild_id][cmd_name] = {
        "code": code,
        "description": description or f"Dynamic command: {cmd_name}",
    }

    guild_data = await load_guild_data(guild_id)
    if "dynamic_commands" not in guild_data:
        guild_data["dynamic_commands"] = {}
    guild_data["dynamic_commands"][cmd_name] = {
        "code": code,
        "description": description or f"Dynamic command: {cmd_name}",
    }

    ok = await save_guild_data(guild_id, guild_data)
    if ok:
        log.info("Saved dynamic command %s/%s to guild's own bin", guild_id, cmd_name)
    return ok

async def delete_dynamic_command_from_store(guild_id: str, cmd_name: str) -> bool:
    if guild_id in dynamic_commands_cache:
        dynamic_commands_cache[guild_id].pop(cmd_name, None)

    guild_data = await load_guild_data(guild_id)
    if "dynamic_commands" in guild_data:
        guild_data["dynamic_commands"].pop(cmd_name, None)
        return await save_guild_data(guild_id, guild_data)
    return True

# ---------------- Safety: AST validation ----------------
class UnsafeCodeError(Exception):
    pass

class SimpleASTValidator(ast.NodeVisitor):
    def visit_Call(self, node):
        if isinstance(node.func, ast.Name) and node.func.id in (
            "eval",
            "exec",
            "__import__",
            "compile",
            "execfile",
        ):
            raise UnsafeCodeError(f"Call to {node.func.id} not allowed.")
        self.generic_visit(node)

    def visit_Attribute(self, node):
        if isinstance(node.attr, str) and node.attr.startswith("__"):
            raise UnsafeCodeError("Access to dunder attributes not allowed.")
        self.generic_visit(node)

def validate_user_code(code: str):
    try:
        tree = ast.parse(textwrap.dedent(code))
    except Exception as e:
        raise UnsafeCodeError(f"Invalid Python syntax: {e}")
    SimpleASTValidator().visit(tree)
    if len(code) > 10000:
        raise UnsafeCodeError("Code too long (max 10000 characters).")
    found = False
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "run":
            found = True
            break
    if not found:
        raise UnsafeCodeError("You must define `async def run(interaction)` or `def run(interaction)`.")

# ---------------- Permission checks ----------------
def is_admin(interaction: discord.Interaction) -> bool:
    try:
        return interaction.user.guild_permissions.manage_guild or interaction.user.guild_permissions.administrator
    except Exception:
        return False

def is_moderator(interaction: discord.Interaction) -> bool:
    try:
        return interaction.user.guild_permissions.moderate_members or interaction.user.guild_permissions.administrator
    except Exception:
        return False

def is_owner(interaction: discord.Interaction) -> bool:
    try:
        return interaction.user.id == interaction.guild.owner_id
    except Exception:
        return False

async def run_blocking(func, *args, timeout=60, **kwargs):
    loop = asyncio.get_running_loop()
    return await asyncio.wait_for(loop.run_in_executor(None, lambda: func(*args, **kwargs)), timeout=timeout)

# ---------------- AI helpers ----------------
async def ai_chat(prompt: str, system_message: str = None) -> str:
    if not OPENAI_API_KEY:
        return "❌ AI not configured. Please set OPENAI_API_KEY."

    try:
        # Try to use OpenAI official async client if available.
        # Keep this flexible to avoid a hard dependency in case of different SDKs.
        from openai import AsyncOpenAI
        client = AsyncOpenAI(api_key=OPENAI_API_KEY)

        messages = []
        if system_message:
            messages.append({"role": "system", "content": system_message})
        messages.append({"role": "user", "content": prompt})

        response = await client.chat.completions.create(
            model="gpt-4o-mini",
            messages=messages,
            max_tokens=2000,
            temperature=0.7,
        )
        return response.choices[0].message.content

    except Exception as e:
        log.exception("AI chat error")
        # Fallback: return the error so the caller can show it to the user.
        return f"❌ AI error: {str(e)}"

async def ai_generate_code(description: str) -> tuple[Optional[str], Optional[str], str]:
    """
    Generate command code from natural language description.
    Returns: (command_name, code, description_or_error)
    """
    if not OPENAI_API_KEY:
        return None, None, "❌ AI not configured. Please set OPENAI_API_KEY."

    system_prompt = textwrap.dedent("""
    You are a Discord bot command generator. Generate Python code for Discord slash commands.

    Rules:
    1. Code MUST define a complete `async def run(interaction)` function
    2. Use interaction.response.send_message() to respond
    3. You can use discord.Embed, discord.ui.Button, discord.ui.Select for rich interactions
    4. Access bot via the global `bot` variable
    5. Access discord module via global `discord` variable
    6. Import statements ARE allowed - you can import any standard or installed libraries
    7. Keep code safe and simple
    8. For buttons/selects, use discord.ui.View
    9. Do NOT use modals or text inputs
    10. ALWAYS include the COMPLETE function - do NOT truncate or use ellipsis

    Return format:
    COMMAND_NAME: <command_name>
    DESCRIPTION: <short description>
    CODE:
    ```python
    async def run(interaction):
        # Your complete code here
        await interaction.response.send_message("...")
    ```
    
    IMPORTANT: Make sure the code is COMPLETE and includes the entire async def run(interaction) function.
    """)

    try:
        # Use higher token limit to ensure complete code generation
        from openai import AsyncOpenAI
        client = AsyncOpenAI(api_key=OPENAI_API_KEY)

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": f"Generate a Discord bot command for: {description}"}
        ]

        response_obj = await client.chat.completions.create(
            model="gpt-4o-mini",
            messages=messages,
            max_tokens=3000,  # Increased from default to ensure complete code
            temperature=0.7,
        )
        response = response_obj.choices[0].message.content

        # Try to extract COMMAND_NAME and DESCRIPTION using regex
        cmd_name = None
        cmd_desc = None

        m_name = re.search(r"COMMAND_NAME\s*:\s*(.+)", response, flags=re.IGNORECASE)
        if m_name:
            cmd_name = m_name.group(1).strip().lower().replace(" ", "_")

        m_desc = re.search(r"DESCRIPTION\s*:\s*(.+)", response, flags=re.IGNORECASE)
        if m_desc:
            cmd_desc = m_desc.group(1).strip()

        # Extract code block (```python ... ``` or ``` ... ```)
        code = None
        m_code = re.search(r"```(?:python)?\s*([\s\S]+?)\s*```", response, flags=re.IGNORECASE)
        if m_code:
            code = m_code.group(1).strip()
        else:
            # Fallback: look for lines after 'CODE:' marker
            m_block = re.split(r"CODE\s*:\s*", response, flags=re.IGNORECASE)
            if len(m_block) > 1:
                code = m_block[1].strip()
                # strip any leading markers
                if code.startswith("```"):
                    code = code.strip("`\n \r")

        # As a last fallback, if the whole response looks like code, use it
        if not code and response.strip().startswith("async def"):
            code = response.strip()

        if not cmd_name:
            # attempt to guess from short description
            guessed = re.sub(r"[^a-zA-Z0-9_ ]", "", description).strip().split()[0:3]
            if guessed:
                cmd_name = "_".join(guessed).lower()
            else:
                cmd_name = "ai_generated_command"

        if not cmd_desc:
            cmd_desc = (description[:100] + "...") if description else "AI generated command"

        if not code:
            return None, None, "AI did not return any code. Response:\n" + response[:1000]

        # Validate that code contains async def run
        if "async def run" not in code and "def run" not in code:
            return None, None, f"Generated code is incomplete. Missing run() function. Response:\n{response[:1000]}"

        # Trim code to reasonable size
        if len(code) > 9500:
            code = code[:9500]

        return cmd_name, code, cmd_desc

    except Exception as e:
        log.exception("AI code generation error")
        return None, None, f"❌ AI error: {str(e)}"

async def ai_fix_code_error(code: str, error: str) -> str:
    """Use AI to suggest fixes for code errors."""
    if not OPENAI_API_KEY:
        return "AI not configured to provide fixes."

    prompt = f"""Fix this Discord bot command code that has an error:

ERROR: {error}

CODE:
```python
{code}
```

Provide the corrected code and explain the fix."""

    return await ai_chat(prompt, "You are a Python debugging expert. Fix the code and explain the issue.")

# ---------------- Dynamic command registration ----------------
async def register_dynamic_command(guild_id: str, cmd_name: str, code: str, description: str = None) -> tuple[bool, Optional[str]]:
    try:
        validate_user_code(code)
    except UnsafeCodeError as e:
        return False, str(e)

    namespace: Dict[str, Any] = {}

    globals_for_exec = {
        "__builtins__": __builtins__,
        "asyncio": asyncio,
        "discord": discord,
        "bot": bot,
    }

    try:
        exec(textwrap.dedent(code), globals_for_exec, namespace)
    except Exception as e:
        error_msg = f"Execution error: {str(e)}\n{traceback.format_exc()}"
        log.exception("Exec error registering %s/%s", guild_id, cmd_name)
        return False, error_msg

    run_func = namespace.get("run")
    if not run_func or not callable(run_func):
        return False, "No callable run(interaction) function found in code."

    # Find the discord.Guild object if the bot is in it
    guild_obj = None
    try:
        guild_obj = discord.utils.get(bot.guilds, id=int(guild_id))
    except Exception:
        guild_obj = None

    # Remove existing if present
    existing = None
    try:
        existing = bot.tree.get_command(cmd_name, guild=guild_obj)
    except Exception:
        existing = None

    if existing:
        try:
            bot.tree.remove_command(cmd_name, guild=guild_obj)
        except Exception:
            log.exception("Failed to remove existing command %s/%s", guild_id, cmd_name)

    async def callback(interaction: discord.Interaction):
        try:
            if inspect.iscoroutinefunction(run_func):
                result = await asyncio.wait_for(run_func(interaction), timeout=30)
            else:
                result = await run_blocking(run_func, interaction, timeout=30)

            if result and not interaction.response.is_done():
                await interaction.response.send_message(str(result), ephemeral=True)
        except asyncio.TimeoutError:
            if not interaction.response.is_done():
                await interaction.response.send_message("⏱️ Command timed out.", ephemeral=True)
        except Exception as e:
            log.exception("Error running dynamic command %s/%s", guild_id, cmd_name)
            try:
                if not interaction.response.is_done():
                    await interaction.response.send_message(f"❌ Error: {e}", ephemeral=True)
            except Exception:
                pass

    desc = (description or f"Dynamic command: {cmd_name}")[:100]
    cmd = app_commands.Command(name=cmd_name, description=desc, callback=callback)

    try:
        bot.tree.add_command(cmd, guild=guild_obj)
    except Exception as e:
        log.exception("Failed to add command %s/%s", guild_id, cmd_name)
        return False, f"Failed to add command: {str(e)}"

    if guild_id not in dynamic_commands_cache:
        dynamic_commands_cache[guild_id] = {}
    dynamic_commands_cache[guild_id][cmd_name] = {
        "code": code,
        "description": desc,
    }

    log.info("Registered dynamic command %s/%s", guild_id, cmd_name)
    return True, None

async def sync_all_guild_commands():
    try:
        for g in bot.guilds:
            await bot.tree.sync(guild=g)
        await bot.tree.sync()
        log.info("All commands synced.")
    except Exception:
        log.exception("Command sync failed")

# ---------------- Events ----------------
@bot.event
async def on_ready():
    log.info("Bot ready: %s (%s)", bot.user, bot.user.id)
    # Load bin configs and register dynamic commands in background
    try:
        await load_all_from_bin()

        # register cached commands
        for gid, group in (dynamic_commands_cache or {}).items():
            for name, data in (group or {}).items():
                if isinstance(data, dict):
                    code = data.get("code", "")
                    desc = data.get("description", f"Dynamic command: {name}")
                else:
                    code = data
                    desc = f"Dynamic command: {name}"
                try:
                    await register_dynamic_command(gid, name, code, desc)
                except Exception:
                    log.exception("Failed to register %s/%s", gid, name)

        await sync_all_guild_commands()
    except Exception:
        log.exception("Error in on_ready setup")

@bot.event
async def on_guild_join(guild: discord.Guild):
    log.info("Bot joined new guild: %s (%s)", guild.name, guild.id)
    try:
        owner = guild.owner
        if not owner:
            log.warning("Could not find owner for guild %s", guild.id)
            return

        embed = discord.Embed(
            title=f"{EMOJI_BELL} Welcome to {bot.user.name}!",
            description="Thank you for adding me to your server! To get started, you need to set up your own data storage.",
            color=discord.Color.blue(),
        )

        embed.add_field(
            name=f"{EMOJI_EXCL} Important: Server-Specific Storage",
            value=(
                "Each server needs its own JSONBin storage to keep your data private and secure. "
                "All server settings, custom commands, join/leave messages, and configurations "
                "will be stored in YOUR own JSONBin account."
            ),
            inline=False,
        )

        embed.add_field(
            name=f"{EMOJI_CODE} Step 1: Create JSONBin Account",
            value=(
                "1. Go to https://jsonbin.io and create a free account\n"
                "2. Create a new bin with an empty object: `{}`\n"
                "3. Copy your **Bin ID** and **Master Key**"
            ),
            inline=False,
        )

        embed.add_field(
            name=f"{EMOJI_BIN2} Step 2: Configure Your Server",
            value=(
                f"Use these commands to set up your storage:\n"
                f"`/setup_jsonbin bin_id:<YOUR_BIN_ID> master_key:<YOUR_MASTER_KEY>`\n\n"
                f"**Server ID:** `{guild.id}`"
            ),
            inline=False,
        )

        embed.add_field(
            name=f"{EMOJI_CHECK} What's Next?",
            value=(
                "Once configured, you can use all bot features:\n"
                "• Create custom commands with `/create_command` or `/describe_command`\n"
                "• Set up welcome/goodbye messages\n"
                "• Configure AI moderation\n"
                "• And much more!"
            ),
            inline=False,
        )

        embed.set_footer(text="Need help? Use /help in your server")

        await owner.send(embed=embed)
        log.info("Sent setup instructions to owner of %s", guild.name)

    except discord.Forbidden:
        log.warning("Cannot DM owner of guild %s - DMs disabled", guild.id)
    except Exception:
        log.exception("Error sending setup DM for guild %s", guild.id)


@bot.event
async def on_member_join(member: discord.Member):
    gid = str(member.guild.id)
    guild_data = guild_cache.get(gid) or await load_guild_data(gid)
    join_ch = guild_data.get("join_channel")
    if join_ch:
        ch = member.guild.get_channel(int(join_ch))
        if ch and isinstance(ch, discord.TextChannel) and ch.permissions_for(member.guild.me).send_messages:
            msg = guild_data.get("join_message", f"👋 Welcome {member.mention} to **{member.guild.name}**!")
            msg = msg.replace("{user}", member.mention).replace("{server}", member.guild.name)
            await ch.send(msg)
    role_id = guild_data.get("auto_role")
    if role_id:
        role = member.guild.get_role(int(role_id))
        if role and member.guild.me.guild_permissions.manage_roles:
            try:
                await member.add_roles(role, reason="Auto-role on join")
            except Exception:
                log.exception("Failed to assign auto-role")

@bot.event
async def on_member_remove(member: discord.Member):
    gid = str(member.guild.id)
    guild_data = guild_cache.get(gid) or await load_guild_data(gid)
    leave_ch = guild_data.get("leave_channel")
    if leave_ch:
        ch = member.guild.get_channel(int(leave_ch))
        if ch and isinstance(ch, discord.TextChannel) and ch.permissions_for(member.guild.me).send_messages:
            msg = guild_data.get("leave_message", f"<:Pikasad:1429499744115228855> {member.name} has left the server.")
            msg = msg.replace("{user}", member.name).replace("{server}", member.guild.name)
            await ch.send(msg)

@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return

    if not message.guild:
        return

    gid = str(message.guild.id)
    guild_data = guild_cache.get(gid) or await load_guild_data(gid)

    if guild_data.get("ai_moderation", False):
        content = message.content.lower()
        if any(word in content for word in ["spam", "scam"]):
            if message.guild.me.guild_permissions.manage_messages:
                try:
                    await message.delete()
                    await message.channel.send(f"{message.author.mention} Your message was removed by AI moderation.", delete_after=5)
                except Exception:
                    log.exception("AI moderation delete failed")

    await bot.process_commands(message)

# ---------------- Commands: storage setup & AI ----------------
@bot.tree.command(name="setup_jsonbin", description="Configure your server's JSONBin storage (Owner only) (If you are scamed by a member of our team go to jsonbin.io and reset your master key)")
@app_commands.describe(
    bin_id="Your JSONBin bin ID",
    master_key="Your JSONBin master key",
)
async def setup_jsonbin(interaction: discord.Interaction, bin_id: str, master_key: str):
    if not is_owner(interaction):
        return await interaction.response.send_message(f"{EMOJI_LOCK} Server owner only.", ephemeral=True)

    await interaction.response.defer(thinking=True, ephemeral=True)

    guild_id = str(interaction.guild.id)

    # Test the credentials by attempting to read
    async with aiohttp.ClientSession() as session:
        url = f"{JSONBIN_BASE}/{bin_id}/latest"
        headers = {"X-Master-Key": master_key}
        try:
            async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status != 200:
                    return await interaction.followup.send(
                        f"❌ Invalid credentials or bin not found. Status: {resp.status}\n"
                        f"Please verify your Bin ID and Master Key.",
                        ephemeral=True,
                    )
        except Exception as e:
            return await interaction.followup.send(
                f"❌ Failed to connect to JSONBin: {str(e)}",
                ephemeral=True,
            )

    # Save the configuration
    ok = await save_guild_bin_config(guild_id, bin_id, master_key)
    if not ok:
        return await interaction.followup.send("❌ Failed to save configuration. Please try again.", ephemeral=True)

    # Initialize empty guild data in the new bin
    guild_data = {}
    guild_cache[guild_id] = guild_data
    await save_guild_data(guild_id, guild_data)

    embed = discord.Embed(
        title=f"{EMOJI_CHECK} JSONBin Configured!",
        description=f"Your server's data storage is now set up and ready to use.",
        color=discord.Color.green(),
    )
    embed.add_field(
        name="What's Next?",
        value=(
            "You can now use all bot features:\n"
            "• `/create_command` or `/describe_command` - Create custom commands\n"
            "• `/join` and `/leave` - Set up welcome/goodbye messages\n"
            "• `/toggle_moderation` - Enable AI moderation\n"
            "• And much more!"
        ),
        inline=False,
    )

    await interaction.followup.send(embed=embed, ephemeral=True)
    log.info("Configured JSONBin for guild %s", guild_id)

# /help command
@bot.tree.command(name="help", description="Bot help")
async def cmd_help(interaction: discord.Interaction):
    await interaction.response.send_message("""Need help?
    we are always ready to provide?
links 
support server 
https://discord.gg/j7Ap4xUkG7
command-generator 
https://qubeia.my.canva.site/command-generator
ToS & Privacy policiy
https://qubeia.my.canva.site
Forums
https://qubeia-forums.my.canva.site/forums""", ephemeral=True)

user_chat_history = {}

@bot.tree.command(name="chat", description="Chat with AI")
@app_commands.describe(message="Your message to the AI")
async def cmd_chat(interaction: discord.Interaction, message: str):
    await interaction.response.defer(thinking=True)

    # Load user's history
    history = user_chat_history.get(interaction.user.id, [])
    history.append({"role": "user", "content": message})

    # Call AI
    response = await ai_chat(message, system_message="You are a helpful assistant.")

    # Append AI response to history
    history.append({"role": "assistant", "content": response})
    user_chat_history[interaction.user.id] = history

    class ContinueView(View):
        def __init__(self):
            super().__init__(timeout=300)  # 5 minutes

        @button(label="Continue Chat", style=discord.ButtonStyle.green)
        async def continue_button(self, button: Button, button_interaction: discord.Interaction):
            await button_interaction.response.send_message(
                "Send your next message with `/chat message:<your text>`",
                ephemeral=True
            )

    await interaction.followup.send(f"💬 {response}", view=ContinueView())
    

@bot.tree.command(name="code", description="Get coding help from AI")
@app_commands.describe(question="Your coding question")
async def cmd_code(interaction: discord.Interaction, question: str):
    await interaction.response.defer(thinking=True)
    system = "You are a helpful coding assistant. Provide clear, concise code examples and explanations."
    response = await ai_chat(question, system)
    await interaction.followup.send(f"💻 {response[:1900]}")

@bot.tree.command(name="describe_command", description="Generate a command from description using AI")
@app_commands.describe(description="Describe what the command should do")
async def describe_command(interaction: discord.Interaction, description: str):
    if not is_admin(interaction):
        return await interaction.response.send_message(f"{EMOJI_LOCK} Admin only.", ephemeral=True)

    guild_id = str(interaction.guild.id)
    config = await get_guild_bin_config(guild_id)
    if not config:
        return await interaction.response.send_message(
            f"{EMOJI_EXCL} This server needs JSONBin setup first!\n"
            f"Server owner should use `/setup_jsonbin` to configure storage.",
            ephemeral=True,
        )

    await interaction.response.defer(thinking=True, ephemeral=True)

    cmd_name, code, cmd_desc = await ai_generate_code(description)

    if not cmd_name or not code:
        return await interaction.followup.send(f"❌ Failed to generate command: {cmd_desc}", ephemeral=True)

    success, error = await register_dynamic_command(guild_id, cmd_name, code, cmd_desc)

    if not success:
        ai_fix = await ai_fix_code_error(code, error)
        return await interaction.followup.send(
            f"""❌ Generated command has errors:\n```
{error[:500]}
```\n\n**AI Suggestion:**\n{ai_fix[:1000]}""",
            ephemeral=True,
        )

    saved = await save_dynamic_command(guild_id, cmd_name, code, cmd_desc)
    if not saved:
        return await interaction.followup.send("⚠️ Command created but failed to save to storage.", ephemeral=True)

    await sync_all_guild_commands()

    embed = discord.Embed(
        title=f"{EMOJI_THINK} Command Generated!",
        description=f"**Name:** `{cmd_name}`\n**Description:** {cmd_desc}",
        color=discord.Color.green(),
    )
    preview = code if len(code) <= 1000 else code[:1000] + "\n..."
    embed.add_field(name="Code Preview", value=f"```python\n{preview}\n```", inline=False)
    await interaction.followup.send(embed=embed, ephemeral=True)

@bot.tree.command(name="create_command", description="Upload a .py file to create a custom command")
@app_commands.describe(file="A .py file that defines async def run(interaction)")
async def create_command(interaction: discord.Interaction, file: discord.Attachment):
    if not is_admin(interaction):
        return await interaction.response.send_message(f"{EMOJI_LOCK} Admin only.", ephemeral=True)

    guild_id = str(interaction.guild.id)
    config = await get_guild_bin_config(guild_id)
    if not config:
        return await interaction.response.send_message(
            f"{EMOJI_EXCL} This server needs JSONBin setup first!\n"
            f"Server owner should use `/setup_jsonbin` to configure storage.",
            ephemeral=True,
        )

    if not file.filename.endswith(".py"):
        return await interaction.response.send_message(f"{EMOJI_CODE} Upload a .py file.", ephemeral=True)

    await interaction.response.defer(thinking=True, ephemeral=True)

    try:
        content = await file.read()
        code = content.decode("utf-8")
        cmd_name = file.filename.rsplit(".", 1)[0].lower()

        success, error = await register_dynamic_command(guild_id, cmd_name, code)

        if not success:
            ai_fix = await ai_fix_code_error(code, error)
            embed = discord.Embed(
                title=f"{EMOJI_EXCL} Command Error",
                description="Your command has errors:",
                color=discord.Color.red(),
            )
            embed.add_field(name="Error", value=f"``\n{error[:500]}\n```", inline=False)
            embed.add_field(name="AI-Suggested Fix", value=ai_fix[:1000], inline=False)
            return await interaction.followup.send(embed=embed, ephemeral=True)

        saved = await save_dynamic_command(guild_id, cmd_name, code)
        if not saved:
            return await interaction.followup.send("⚠️ Command created but failed to save.", ephemeral=True)

        await sync_all_guild_commands()
        await interaction.followup.send(f"{EMOJI_THINK} Command `{cmd_name}` created successfully!", ephemeral=True)

    except Exception as e:
        log.exception("create_command error")
        return await interaction.followup.send(f"❌ Error: {str(e)}", ephemeral=True)

@bot.tree.command(name="delete_command", description="Delete a custom command")
@app_commands.describe(command_name="Name of the command to delete")
async def delete_command(interaction: discord.Interaction, command_name: str):
    if not is_admin(interaction):
        return await interaction.response.send_message(f"{EMOJI_LOCK} Admin only.", ephemeral=True)

    guild_id = str(interaction.guild.id)
    guild_obj = interaction.guild

    if bot.tree.get_command(command_name, guild=guild_obj):
        try:
            bot.tree.remove_command(command_name, guild=guild_obj)
        except Exception:
            log.exception("Failed to remove command")

    ok = await delete_dynamic_command_from_store(guild_id, command_name)
    if not ok:
        return await interaction.response.send_message("⚠️ Failed to delete from storage.", ephemeral=True)

    await sync_all_guild_commands()
    await interaction.response.send_message(f"{EMOJI_BIN} Command `{command_name}` deleted.", ephemeral=True)

@bot.tree.command(name="rename_command", description="Rename a custom command")
@app_commands.describe(old_name="Current command name", new_name="New command name")
async def rename_command(interaction: discord.Interaction, old_name: str, new_name: str):
    if not is_admin(interaction):
        return await interaction.response.send_message(f"{EMOJI_LOCK} Admin only.", ephemeral=True)

    guild_id = str(interaction.guild.id)

    if guild_id not in dynamic_commands_cache or old_name not in dynamic_commands_cache[guild_id]:
        return await interaction.response.send_message(f"{EMOJI_EXCL} Command `{old_name}` not found.", ephemeral=True)

    cmd_data = dynamic_commands_cache[guild_id][old_name]
    code = cmd_data.get("code") if isinstance(cmd_data, dict) else cmd_data
    desc = cmd_data.get("description", f"Dynamic command: {new_name}") if isinstance(cmd_data, dict) else f"Dynamic command: {new_name}"

    guild_obj = interaction.guild
    if bot.tree.get_command(old_name, guild=guild_obj):
        bot.tree.remove_command(old_name, guild=guild_obj)

    await delete_dynamic_command_from_store(guild_id, old_name)

    success, error = await register_dynamic_command(guild_id, new_name, code, desc)
    if not success:
        return await interaction.response.send_message(f"❌ Error: {error}", ephemeral=True)

    await save_dynamic_command(guild_id, new_name, code, desc)
    await sync_all_guild_commands()

    await interaction.response.send_message(f"{EMOJI_BIN2} Command `{old_name}` renamed to `{new_name}`.", ephemeral=True)

@bot.tree.command(name="rename_command_description", description="Update a command's description")
@app_commands.describe(command_name="Command name", new_description="New description")
async def rename_command_description(interaction: discord.Interaction, command_name: str, new_description: str):
    if not is_admin(interaction):
        return await interaction.response.send_message(f"{EMOJI_LOCK} Manage server permission required.", ephemeral=True)

    guild_id = str(interaction.guild.id)

    if guild_id not in dynamic_commands_cache or command_name not in dynamic_commands_cache[guild_id]:
        return await interaction.response.send_message(f"{EMOJI_EXCL} Command `{command_name}` not found.", ephemeral=True)

    cmd_data = dynamic_commands_cache[guild_id][command_name]
    code = cmd_data.get("code") if isinstance(cmd_data, dict) else cmd_data

    guild_obj = interaction.guild
    if bot.tree.get_command(command_name, guild=guild_obj):
        bot.tree.remove_command(command_name, guild=guild_obj)

    success, error = await register_dynamic_command(guild_id, command_name, code, new_description)
    if not success:
        return await interaction.response.send_message(f"❌ Error: {error}", ephemeral=True)

    await save_dynamic_command(guild_id, command_name, code, new_description)
    await sync_all_guild_commands()

    await interaction.response.send_message(f"{EMOJI_CHECK} Description updated for `{command_name}`.", ephemeral=True)

@bot.tree.command(name="list_commands", description="List all custom commands")
async def list_commands(interaction: discord.Interaction):
    guild_id = str(interaction.guild.id)

    if guild_id not in dynamic_commands_cache or not dynamic_commands_cache[guild_id]:
        return await interaction.response.send_message("No custom commands in this server.", ephemeral=True)

    embed = discord.Embed(title=f"{EMOJI_BIN2} Custom Commands", color=discord.Color.blue())

    for cmd_name, cmd_data in dynamic_commands_cache[guild_id].items():
        desc = cmd_data.get("description", "No description") if isinstance(cmd_data, dict) else "No description"
        embed.add_field(name=f"/{cmd_name}", value=desc, inline=False)

    await interaction.response.send_message(embed=embed, ephemeral=True)

@bot.tree.command(name="toggle_moderation", description="Toggle AI moderation (Moderate Members permission)")
async def toggle_moderation(interaction: discord.Interaction):
    if not is_moderator(interaction):
        return await interaction.response.send_message(f"{EMOJI_LOCK} Moderate Members permission required.", ephemeral=True)

    guild_id = str(interaction.guild.id)
    guild_data = guild_cache.get(guild_id) or await load_guild_data(guild_id)

    current = guild_data.get("ai_moderation", False)
    guild_data["ai_moderation"] = not current

    await save_guild_data(guild_id, guild_data)

    status = "enabled" if guild_data["ai_moderation"] else "disabled"
    await interaction.response.send_message(f"{EMOJI_HAMMER} AI moderation {status}.", ephemeral=True)

@bot.tree.command(name="name", description="Change bot nickname in this server (Owner only)")
@app_commands.describe(nickname="New nickname for the bot")
async def cmd_name(interaction: discord.Interaction, nickname: str):
    if not is_owner(interaction):
        return await interaction.response.send_message(f"{EMOJI_LOCK} Server owner only.", ephemeral=True)

    try:
        await interaction.guild.me.edit(nick=nickname)
        await interaction.response.send_message(f"{EMOJI_CHECK} Bot nickname changed to: {nickname}", ephemeral=True)
    except Exception as e:
        await interaction.response.send_message(f"❌ Failed to change nickname: {str(e)}", ephemeral=True)

@bot.tree.command(name="join", description="Set welcome channel")
async def cmd_join(interaction: discord.Interaction, channel: discord.TextChannel):
    if not is_admin(interaction):
        return await interaction.response.send_message(f"{EMOJI_LOCK} Admin only.", ephemeral=True)

    gid = str(interaction.guild.id)
    guild_data = guild_cache.get(gid) or await load_guild_data(gid)
    guild_data["join_channel"] = str(channel.id)
    await save_guild_data(gid, guild_data)
    await interaction.response.send_message(f"{EMOJI_BELL} Welcome messages set to {channel.mention}", ephemeral=True)

@bot.tree.command(name="unjoin", description="Disable welcome messages")
async def cmd_unjoin(interaction: discord.Interaction):
    if not is_admin(interaction):
        return await interaction.response.send_message(f"{EMOJI_LOCK} Admin only.", ephemeral=True)

    gid = str(interaction.guild.id)
    guild_data = guild_cache.get(gid) or await load_guild_data(gid)
    guild_data.pop("join_channel", None)
    await save_guild_data(gid, guild_data)
    await interaction.response.send_message(f"{EMOJI_CLOCK} Welcome disabled.", ephemeral=True)

@bot.tree.command(name="leave", description="Set goodbye channel")
async def cmd_leave(interaction: discord.Interaction, channel: discord.TextChannel):
    if not is_admin(interaction):
        return await interaction.response.send_message(f"{EMOJI_LOCK} Admin only.", ephemeral=True)

    gid = str(interaction.guild.id)
    guild_data = guild_cache.get(gid) or await load_guild_data(gid)
    guild_data["leave_channel"] = str(channel.id)
    await save_guild_data(gid, guild_data)
    await interaction.response.send_message(f"{EMOJI_BELL} Leave messages set to {channel.mention}", ephemeral=True)

@bot.tree.command(name="unleave", description="Disable goodbye messages")
async def cmd_unleave(interaction: discord.Interaction):
    if not is_admin(interaction):
        return await interaction.response.send_message(f"{EMOJI_LOCK} Admin only.", ephemeral=True)

    gid = str(interaction.guild.id)
    guild_data = guild_cache.get(gid) or await load_guild_data(gid)
    guild_data.pop("leave_channel", None)
    await save_guild_data(gid, guild_data)
    await interaction.response.send_message(f"{EMOJI_CLOCK} Leave disabled.", ephemeral=True)

@bot.tree.command(name="join_message", description="Set custom welcome message")
@app_commands.describe(message="Message ({user} and {server} are replaced)")
async def cmd_join_message(interaction: discord.Interaction, message: str):
    if not is_admin(interaction):
        return await interaction.response.send_message(f"{EMOJI_LOCK} Admin only.", ephemeral=True)

    gid = str(interaction.guild.id)
    guild_data = guild_cache.get(gid) or await load_guild_data(gid)
    guild_data["join_message"] = message
    await save_guild_data(gid, guild_data)
    await interaction.response.send_message(f"{EMOJI_MESSAGE} Welcome message updated.", ephemeral=True)

@bot.tree.command(name="leave_message", description="Set custom goodbye message")
@app_commands.describe(message="Message ({user} and {server} are replaced)")
async def cmd_leave_message(interaction: discord.Interaction, message: str):
    if not is_admin(interaction):
        return await interaction.response.send_message(f"{EMOJI_LOCK} Admin only.", ephemeral=True)

    gid = str(interaction.guild.id)
    guild_data = guild_cache.get(gid) or await load_guild_data(gid)
    guild_data["leave_message"] = message
    await save_guild_data(gid, guild_data)
    await interaction.response.send_message(f"{EMOJI_MESSAGE} Goodbye message updated.", ephemeral=True)

@bot.tree.command(name="role_assign_on_join", description="Assign a role to new members")
async def cmd_role_assign(interaction: discord.Interaction, role: discord.Role):
    if not is_admin(interaction):
        return await interaction.response.send_message(f"{EMOJI_LOCK} Admin only.", ephemeral=True)

    gid = str(interaction.guild.id)
    guild_data = guild_cache.get(gid) or await load_guild_data(gid)
    guild_data["auto_role"] = str(role.id)
    await save_guild_data(gid, guild_data)
    await interaction.response.send_message(f"{EMOJI_GAME} New members will receive `{role.name}`", ephemeral=True)

@bot.tree.command(name="unroleassignonjoin", description="Disable auto role assign")
async def cmd_unroleassign(interaction: discord.Interaction):
    if not is_admin(interaction):
        return await interaction.response.send_message(f"{EMOJI_LOCK} Admin only.", ephemeral=True)

    gid = str(interaction.guild.id)
    guild_data = guild_cache.get(gid) or await load_guild_data(gid)
    guild_data.pop("auto_role", None)
    await save_guild_data(gid, guild_data)
    await interaction.response.send_message(f"{EMOJI_HAMMER} Auto-role disabled.", ephemeral=True)

@bot.tree.command(name="boosts", description="Send boost info")
async def cmd_boosts(interaction: discord.Interaction, channel: discord.TextChannel):
    if not is_admin(interaction):
        return await interaction.response.send_message(f"{EMOJI_LOCK} Admin only.", ephemeral=True)

    count = interaction.guild.premium_subscription_count
    await channel.send(f"<a:Boost:1428980068285157507> This server has **{count} boosts**!")
    await interaction.response.send_message(f"{EMOJI_CHECK} Sent boost info to {channel.mention}", ephemeral=True)

@bot.tree.command(name="unboosts", description="Send a message indicating boost info cleared")
async def cmd_unboosts(interaction: discord.Interaction, channel: discord.TextChannel):
    if not is_admin(interaction):
        return await interaction.response.send_message(f"{EMOJI_LOCK} Admin only.", ephemeral=True)

    await channel.send("Boost info cleared.")
    await interaction.response.send_message(f"{EMOJI_CHECK} Sent clear message to {channel.mention}", ephemeral=True)

    
# ---------------- Flask Web Service ----------------
from flask import Flask, request, jsonify
from threading import Thread

app = Flask(__name__)

@app.route("/describe_command", methods=["POST"])
def describe_command_endpoint():
    """
    This endpoint receives JSON:
    {
        "guild_id": "123",
        "description": "make a ping command"
    }
    """
    data = request.get_json(force=True)
    guild_id = str(data.get("guild_id"))
    description = data.get("description")

    if not guild_id or not description:
        return jsonify({"error": "guild_id and description are required"}), 400

    # Schedule coroutine in bot's asyncio loop
    async def run_task():
        # Call your bot's internal describe logic:
        name, code, desc = await ai_generate_code(description)
        return {"cmd_name": name, "code": code, "desc": desc}

    future = asyncio.run_coroutine_threadsafe(run_task(), bot.loop)
    result = future.result()

    return jsonify(result), 200


def run_flask():
    # Replit uses PORT env var
    port = int(os.environ.get("PORT", 3000))
    app.run(host="0.0.0.0", port=port)


def start_flask():
    t = Thread(target=run_flask)
    t.daemon = True
    t.start()


# Start Flask BEFORE bot runs
start_flask()


# ---------------- Run ----------------
if __name__ == "__main__":
    try:
        bot.run(DISCORD_TOKEN)
    except Exception:
        log.exception("Failed to start bot")
        raise
