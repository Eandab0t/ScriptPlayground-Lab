"use strict";

class Embed {
  constructor(data = {}) { this.data = { type: "rich", ...data }; }
  setColor(color) { this.data.color = typeof color === "number" ? color : parseInt(String(color).replace("#", ""), 16) || 0; return this; }
  setTitle(t) { this.data.title = t; return this; }
  setDescription(d) { this.data.description = d; return this; }
  setURL(u) { this.data.url = u; return this; }
  setFooter(o) { this.data.footer = { text: o && o.text }; return this; }
  setImage(u) { this.data.image = { url: u }; return this; }
  setThumbnail(u) { this.data.thumbnail = { url: u }; return this; }
  setAuthor(o) { this.data.author = { name: o && o.name, icon_url: o && o.iconURL, url: o && o.url }; return this; }
  setTimestamp(v) { this.data.timestamp = (v instanceof Date ? v : new Date()).toISOString(); return this; }
  addFields(...items) { for (const f of items.flat()) this.data.fields = [...(this.data.fields || []), { name: f.name, value: f.value, inline: !!f.inline }]; return this; }
  addField(name, value, inline) { return this.addFields({ name, value, inline }); }
  toJSON() { return this.data; }
}

class EmbedBuilder extends Embed {}

class SlashCommandBuilder {
  constructor() { this.data = { name: "", description: "", type: 1, options: [] }; }
  setName(n) { this.data.name = n; return this; }
  setDescription(d) { this.data.description = d; return this; }
  _addOption(type, fn) {
    const acc = { name: "", description: "", required: false, choices: [], type };
    const builder = {
      setName: (n) => { acc.name = n; return builder; },
      setDescription: (d) => { acc.description = d; return builder; },
      setRequired: (r = true) => { acc.required = !!r; return builder; },
      addChoice: (name, value) => { acc.choices.push({ name, value }); return builder; },
      setChoices: (...c) => { acc.choices.push(...c.flat()); return builder; },
      setAutocomplete: () => builder,
      setMinLength: () => builder, setMaxLength: () => builder,
      setMinValue: () => builder, setMaxValue: () => builder,
    };
    fn(builder);
    acc.name = String(acc.name).toLowerCase().replace(/[^a-z0-9_]/g, "-");
    this.data.options.push(acc);
    return this;
  }
  addStringOption(fn) { return this._addOption(3, fn); }
  addIntegerOption(fn) { return this._addOption(4, fn); }
  addBooleanOption(fn) { return this._addOption(5, fn); }
  addUserOption(fn) { return this._addOption(6, fn); }
  addChannelOption(fn) { return this._addOption(7, fn); }
  addRoleOption(fn) { return this._addOption(8, fn); }
  addMentionableOption(fn) { return this._addOption(9, fn); }
  addAttachmentOption(fn) { return this._addOption(11, fn); }
  toJSON() { return this.data; }
}

class ButtonBuilder {
  constructor() { this.data = { type: 2, style: 1 }; }
  setCustomId(id) { this.data.custom_id = id; return this; }
  setLabel(l) { this.data.label = l; return this; }
  setStyle(s) { this.data.style = typeof s === "number" ? s : ({ Primary: 1, Secondary: 2, Success: 3, Danger: 4, Link: 5 }[s] || 1); return this; }
  setEmoji(e) { this.data.emoji = { name: typeof e === "string" ? e : e && e.name }; return this; }
  setURL(u) { this.data.url = u; return this; }
  setDisabled(d) { this.data.disabled = !!d; return this; }
  toJSON() { return this.data; }
}

class ActionRowBuilder {
  constructor() { this.data = { type: 1, components: [] }; }
  addComponents(...items) { for (const c of items.flat()) this.data.components.push(c.toJSON ? c.toJSON() : c); return this; }
  addComponent(item) { return this.addComponents(item); }
  toJSON() { return this.data; }
}

class Collection extends Map {}

function _wireUser(d) {
  if (!d) return null;
  const user = {
    id: d.id, username: d.username, tag: `${d.username}#0000`,
    displayName: d.global_name || d.username,
    bot: !!d.bot,
    avatar: d.avatar,
    createdAt: new Date(parseInt(d.id) / 4194304 + 1420070400000),
    createdTimestamp: parseInt(d.id) / 4194304 + 1420070400000,
    displayAvatarURL: () => d.avatar ? `https://cdn.discordapp.com/avatars/${d.id}/${d.avatar}.png` : null,
    toString: () => `<@${d.id}>`,
  };
  user.setActivity = (name, options = {}) => {
    process.stdout.write(JSON.stringify({ type: "presence", activity: name, status: options.status || "online" }) + "\n");
    return user;
  };
  user.setPresence = (options = {}) => user.setActivity(options.activity && options.activity.name, options);
  user.setStatus = (status) => user.setActivity(null, { status });
  return user;
}

/* ------------------------------------------------------------------ client */

class Client {
  constructor(options = {}) {
    this.options = { intents: [], ...options };
    this.handlers = new Map();
    this.user = null;
    this.readyAt = null;
    this._send = (payload) => {
      process.stdout.write(JSON.stringify(payload) + "\n");
    };
    process.stdin.setEncoding("utf8");
    let buffer = "";
    process.stdin.on("data", (chunk) => {
      buffer += chunk;
      let index;
      while ((index = buffer.indexOf("\n")) >= 0) {
        const line = buffer.slice(0, index);
        buffer = buffer.slice(index + 1);
        if (!line.trim()) continue;
        let event;
        try { event = JSON.parse(line); } catch { continue; }
        this._dispatch(event);
      }
    });
    process.stdin.resume();
    /* Handshake: announce the bridge so the host knows the bot booted and can
     * reply with the ready event. Without this, a bot that only registers
     * listeners inside client.on('ready') would deadlock the boot. */
    process.stdout.write(JSON.stringify({ type: "hello" }) + "\n");
    process.stdin.on("end", () => process.exit(0));
  }

  _dispatch(event) {
    if (event.type === "ready") {
      this.user = _wireUser(event.user);
      this.readyAt = new Date();
      /* Give bot 'ready' handlers (e.g. REST command sync) a beat to run,
       * then ack so the host's boot() completes with the command list ready. */
      Promise.resolve().then(async () => {
        for (const handler of this.handlers.get("ready") || []) {
          try { await handler(this); } catch (err) { this.emit("error", err); }
        }
        process.stdout.write(JSON.stringify({ type: "ready" }) + "\n");
      });
      return;
    }
    if (event.type === "message") {
      const message = new Message(this, event);
      this.emit("messageCreate", message);
      this.emit("message", message);
      return;
    }
    if (event.type === "interaction") {
      const interaction = new ChatInputCommandInteraction(this, event);
      this.emit("interactionCreate", interaction);
      return;
    }
  }

  on(name, handler) {
    if (!this.handlers.has(name)) this.handlers.set(name, []);
    this.handlers.get(name).push(handler);
    return this;
  }

  emit(name, ...args) {
    if (name === "ready") return; /* dispatched inline with ack, see _dispatch */
    const handlers = this.handlers.get(name) || [];
    for (const handler of handlers) {
      Promise.resolve().then(() => handler(...args)).catch((err) => {
        if (name === "error" || !this.handlers.get("error")?.length) {
          /* No user error handler: surface it to the simulator host instead
           * of letting it vanish into an unhandled rejection. */
          process.stdout.write(JSON.stringify({ type: "error", message: String((err && err.stack) || err) }) + "\n");
        } else {
          this.emit("error", err);
        }
      });
    }
  }

  once(name, handler) {
    const wrapped = (...args) => { this._off(name, wrapped); handler(...args); };
    return this.on(name, wrapped);
  }

  _off(name, handler) {
    const list = this.handlers.get(name) || [];
    const index = list.indexOf(handler);
    if (index >= 0) list.splice(index, 1);
  }

  off(name, handler) { this._off(name, handler); return this; }
  removeAllListeners() { this.handlers.clear(); return this; }

  async login() { return "offline-simulated-token"; }

  async destroy() {
    process.stdout.write(JSON.stringify({ type: "bye" }) + "\n");
  }

  get ws() { return { destroy: () => {} }; }

  isReady() { return this.readyAt !== null; }

  guilds = { cache: new Collection() };
  channels = { cache: new Collection() };

  _replyLater(payload) { this._send(payload); }
}

/* ------------------------------------------------------------------ structs */

class Message {
  constructor(client, data) {
    this.client = client;
    this.id = data.message_id;
    this.content = data.content || "";
    this.author = _wireUser(data.author);
    this.channelId = data.channel_id;
    this.createdTimestamp = Date.now();
    this.member = data.member ? { ...data.member, user: this.author } : null;
    this.mentions = {
      has: (target) => {
        if (typeof target === "string") {
          return this.content.includes(`<@${target}>`) || this.content.includes(`<@!${target}>`);
        }
        if (target && target.id) {
          return this.content.includes(`<@${target.id}>`) || this.content.includes(`<@!${target.id}>`);
        }
        return false;
      },
    };
    this.embeds = data.embeds || [];
  }

  get channel() {
    const client = this.client;
    return {
      id: this.channelId,
      send: async (payload) => {
        client._send({ type: "send", channel_id: this.channelId, ..._normalizeSend(payload) });
        return { id: null, content: payload && payload.content };
      },
      sendTyping: async () => {
        client._send({ type: "typing", channel_id: this.channelId });
      },
    };
  }

  async reply(payload) {
    this.client._send({ type: "send", channel_id: this.channelId, ..._normalizeSend(payload), reply_to: this.id });
    return { id: null };
  }

  get editable() { return false; }
}

class ChatInputCommandInteraction {
  constructor(client, data) {
    this.client = client;
    this.id = data.interaction_id;
    this.commandName = data.command_name;
    this.user = _wireUser(data.user);
    this.member = data.member ? { ...data.member, user: this.user } : null;
    this.channelId = data.channel_id;
    this._replied = false;
    this._deferred = false;
    this.options = {
      _map: data.options || {},
      getString: (n) => this.options._map[n],
      getInteger: (n) => { const v = this.options._map[n]; return v === undefined ? null : Number(v); },
      getBoolean: (n) => { const v = this.options._map[n]; return v === undefined ? null : !!v; },
      getUser: (n) => _wireUser((data.resolved && data.resolved.users && data.resolved.users[this.options._map[n]]) || null),
      getChannel: () => null, getRole: () => null, getMember: () => null,
      data: [],
    };
  }

  get channel() {
    const client = this.client;
    const channelId = this.channelId;
    return {
      id: channelId,
      send: async (payload) => { client._send({ type: "send", channel_id: channelId, ..._normalizeSend(payload) }); },
    };
  }

  async reply(payload) {
    this._replied = true;
    this.client._send({ type: "interaction_reply", interaction_id: this.id, channel_id: this.channelId, ..._normalizeSend(payload) });
    return {};
  }

  async deferReply(options = {}) {
    this._deferred = true;
    this.client._send({ type: "interaction_defer", interaction_id: this.id, ephemeral: !!options.ephemeral || Number(options.flags) === 64 });
    return {};
  }

  async editReply(payload) {
    this.client._send({ type: "interaction_edit", interaction_id: this.id, ..._normalizeSend(payload) });
    return {};
  }

  async deleteReply() {}
  async followUp(payload) {
    this.client._send({ type: "send", channel_id: this.channelId, ..._normalizeSend(payload) });
    return {};
  }
  isChatInputCommand() { return true; }
  isRepliable() { return true; }
  inCachedGuild() { return true; }
  isCommand() { return this.isChatInputCommand(); }
}

function _normalizeSend(payload) {
  if (payload === null || payload === undefined) return { content: "" };
  if (typeof payload === "string") return { content: payload };
  const out = {};
  if (payload.content !== undefined) out.content = payload.content;
  if (payload.embeds) out.embeds = payload.embeds.map((e) => (e.toJSON ? e.toJSON() : e));
  if (payload.embed) out.embeds = [payload.embed.toJSON ? payload.embed.toJSON() : payload.embed];
  if (payload.ephemeral) out.ephemeral = true;
  if (payload.flags !== undefined) {
    const numeric = typeof payload.flags === "number" ? payload.flags : Number(payload.flags);
    if (Number.isFinite(numeric) && numeric & 64) out.ephemeral = true;
  }
  if (payload.components) {
    out.components = payload.components.map((c) => (c.toJSON ? c.toJSON() : c));
  }
  if (payload.allowedMentions !== undefined) out.allowed_mentions = payload.allowedMentions;
  return out;
}

/* ------------------------------------------------------------------- REST */

class REST {
  constructor() {}
  setToken() { return this; }
  async put(url, options = {}) {
    if (String(url).includes("/applications/") && String(url).endsWith("/commands")) {
      process.stdout.write(JSON.stringify({ type: "sync_commands", commands: options.body || [] }) + "\n");
    }
    return options.body || [];
  }
  async get(url) { return []; }
  async post(url, options = {}) { return options.body || null; }
  async delete() { return null; }
}

const Routes = {
  applicationCommands: (appId) => `/applications/${appId}/commands`,
  applicationGuildCommands: (appId, guildId) => `/applications/${appId}/guilds/${guildId}/commands`,
};

const GatewayIntentBits = {
  Guilds: 1, GuildMembers: 2, GuildMessages: 512, MessageContent: 32768,
  DirectMessages: 4096, GuildMessageReactions: 1024, GuildVoiceStates: 128,
};

const MessageFlags = { Ephemeral: 64, SuppressEmbeds: 4 };

const ActivityType = { Playing: 0, Streaming: 1, Listening: 2, Watching: 3, Competing: 5 };

const Events = {
  ClientReady: "ready", MessageCreate: "messageCreate", InteractionCreate: "interactionCreate",
  Error: "error", Warn: "warn", Debug: "debug",
};

const PermissionFlagsBits = {
  Administrator: 8n, ViewChannel: 1024n, SendMessages: 2048n, ManageMessages: 8192n,
};

const Partials = { Message: 0, Channel: 1, Reaction: 2, User: 3 };

module.exports = {
  Client, Embed: EmbedBuilder, EmbedBuilder, SlashCommandBuilder,
  ButtonBuilder, ActionRowBuilder, Collection, REST, Routes,
  GatewayIntentBits, MessageFlags, ActivityType, Events,
  PermissionFlagsBits, Partials,
  version: "14.0.0-offline-shim",
};

/* child process lifecycle: report readiness of the bridge itself */
process.on("uncaughtException", (err) => {
  process.stdout.write(JSON.stringify({ type: "error", message: String(err && err.stack || err) }) + "\n");
});
process.on("unhandledRejection", (err) => {
  process.stdout.write(JSON.stringify({ type: "error", message: String(err && err.stack || err) }) + "\n");
});
