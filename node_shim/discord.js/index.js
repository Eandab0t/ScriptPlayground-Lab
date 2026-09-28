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
  addNumberOption(fn) { return this._addOption(10, fn); }
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

class Collection extends Map {
  first() { return this.values().next().value; }
  last() { return [...this.values()].at(-1); }
  find(predicate) { for (const [key, value] of this) if (predicate(value, key, this)) return value; }
  map(callback) { return [...this].map(([key, value]) => callback(value, key, this)); }
  filter(predicate) { return new Collection([...this].filter(([key, value]) => predicate(value, key, this))); }
}

function _permissions(bitfield) {
  const bits = BigInt(bitfield || 0);
  return { bitfield: bits,
    has: (permission, checkAdmin = true) => {
      const required = BigInt(permission);
      return (checkAdmin && (bits & PermissionFlagsBits.Administrator) !== 0n)
        || (bits & required) === required;
    },
    any: (permission) => (bits & BigInt(permission)) !== 0n,
    toArray: () => Object.entries(PermissionFlagsBits)
      .filter(([, flag]) => (bits & flag) === flag).map(([name]) => name),
  };
}

function _wireRole(data, guild) {
  const role = { ...data, id: String(data.id || ""), guild,
    permissions: _permissions(data.permissions), mention: `<@&${data.id}>`,
    toString: () => data.name || String(data.id || "") };
  return role;
}

function _wireChannel(client, data = {}) {
  const id = String(data.id || "");
  const permissionOverwrites = new Collection((data.permission_overwrites || []).map((item) => {
    const overwrite = { ...item, id: String(item.id), allow: BigInt(item.allow || 0), deny: BigInt(item.deny || 0) };
    return [overwrite.id, overwrite];
  }));
  const channel = {
    ...data, id, client,
    guild: client.guilds.cache.get(String(data.guild_id || "")) || null,
    permissionOverwrites: { cache: permissionOverwrites },
    permissionsFor(member) {
      if (!member || !member.permissions) return null;
      let bits = member.permissions.bitfield;
      if (member.permissions.has(PermissionFlagsBits.Administrator, false)) {
        return _permissions(Object.values(PermissionFlagsBits).reduce((all, flag) => all | flag, 0n));
      }
      const apply = (allow, deny) => { bits = (bits & ~deny) | allow; };
      const everyone = permissionOverwrites.get(this.guild && this.guild.id);
      if (everyone && everyone.type === 0) apply(everyone.allow, everyone.deny);
      let allow = 0n, deny = 0n;
      for (const role of member.roles.cache.values()) {
        if (String(role.id) === String(this.guild && this.guild.id)) continue;
        const overwrite = permissionOverwrites.get(String(role.id));
        if (overwrite && overwrite.type === 0) {
          allow |= overwrite.allow;
          deny |= overwrite.deny;
        }
      }
      apply(allow, deny);
      const memberOverwrite = permissionOverwrites.get(String(member.id));
      if (memberOverwrite && memberOverwrite.type === 1) apply(memberOverwrite.allow, memberOverwrite.deny);
      return _permissions(bits);
    },
    send: async (payload) => {
      const message = _normalizeSend(payload);
      client._send({ type: "send", channel_id: id, ...message });
      return { id: null, channelId: id, content: message.content || "" };
    },
    sendTyping: async () => client._send({ type: "typing", channel_id: id }),
    toString: () => `#${data.name || id}`,
  };
  return channel;
}

function _wireMember(client, data, guild) {
  if (!data) return null;
  const user = _wireUser(data.user || data);
  const roleIds = [...new Set((data.roles || []).map(String))];
  const roleCache = new Collection(roleIds.map((id) => {
    const key = String(id);
    return [key, guild && guild.roles.cache.get(key) || { id: key, name: "unknown role" }];
  }));
  const roles = {
    cache: roleCache,
    add: async (role) => { const item = typeof role === "object" ? role : guild.roles.cache.get(String(role)); if (item) roleCache.set(String(item.id), item); return roles; },
    remove: async (role) => { roleCache.delete(String(typeof role === "object" ? role.id : role)); return roles; },
  };
  const permissionBits = BigInt(data.permissions || 0);
  return { ...data, id: user.id, user, guild,
    nickname: data.nick || null, displayName: data.nick || user.displayName,
    joinedAt: data.joined_at ? new Date(data.joined_at) : null,
    roles, rolesData: (data.roles || []).map(String), permissions: _permissions(permissionBits), };
}

function _wireGuild(client, data) {
  const guild = { ...data, id: String(data.id || ""), client,
    memberCount: (data.members || []).length,
    members: { cache: new Collection(), fetch: async (id) => {
      const member = guild.members.cache.get(String(id));
      if (!member) throw new Error(`Unknown member ${id}`);
      return member;
    } },
    channels: { cache: new Collection(), fetch: async (id) => {
      const channel = guild.channels.cache.get(String(id));
      if (!channel) throw new Error(`Unknown channel ${id}`);
      return channel;
    } },
    roles: { cache: new Collection() },
    fetch: async () => guild,
  };
  for (const raw of data.roles || []) guild.roles.cache.set(String(raw.id), _wireRole(raw, guild));
  for (const raw of data.members || []) {
    const member = _wireMember(client, raw, guild);
    guild.members.cache.set(member.id, member);
    client.users.cache.set(member.user.id, member.user);
  }
  for (const raw of data.channels || []) {
    const channel = _wireChannel(client, raw);
    channel.guild = guild;
    guild.channels.cache.set(channel.id, channel);
    client.channels.cache.set(channel.id, channel);
  }
  return guild;
}

function _wireUser(d) {
  if (!d) return null;
  const avatar = d.avatar_url || d.avatar;
  const avatarURL = () => !avatar ? null : (/^https?:\/\//.test(avatar)
    ? avatar : `https://cdn.discordapp.com/avatars/${d.id}/${avatar}.png`);
  const bannerURL = () => !d.banner_url && !d.banner ? null
    : d.banner_url || (/^https?:\/\//.test(d.banner) ? d.banner
      : `https://cdn.discordapp.com/banners/${d.id}/${d.banner}.png`);
  const user = {
    id: d.id, username: d.username, tag: `${d.username}#0000`,
    globalName: d.global_name || null,
    displayName: d.global_name || d.username,
    bot: !!d.bot,
    avatar: d.avatar,
    banner: d.banner || null,
    accentColor: d.accent_color ?? null,
    bio: d.bio || "",
    status: d.status || "online",
    createdAt: new Date(parseInt(d.id) / 4194304 + 1420070400000),
    createdTimestamp: parseInt(d.id) / 4194304 + 1420070400000,
    avatarURL,
    displayAvatarURL: avatarURL,
    bannerURL,
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

function _resolveUser(client, data) {
  if (!data) return null;
  return client.users.cache.get(String(data.id)) || _wireUser(data);
}

function _resolveMember(client, data, guildId) {
  if (!data) return null;
  const guild = client.guilds.cache.get(String(guildId || data.guild_id || ""));
  const id = String((data.user && data.user.id) || data.id || "");
  return guild && guild.members.cache.get(id) || _wireMember(client, data, guild);
}

function _resolveChannel(client, data) {
  if (!data) return null;
  const id = String(data.id || "");
  const cached = client.channels.cache.get(id);
  if (cached) return cached;
  const channel = _wireChannel(client, data);
  const guild = client.guilds.cache.get(String(data.guild_id || ""));
  if (guild) {
    channel.guild = guild;
    guild.channels.cache.set(id, channel);
  }
  client.channels.cache.set(id, channel);
  return channel;
}

function _resolveRole(client, data, guildId) {
  if (!data) return null;
  const id = String(data.id || "");
  const guild = client.guilds.cache.get(String(guildId || data.guild_id || ""));
  const cached = guild && guild.roles.cache.get(id);
  if (cached) return cached;
  const permissions = BigInt(data.permissions || 0);
  return { ...data, id, guild, permissions: { bitfield: permissions,
    has: (permission) => (permissions & BigInt(permission)) === BigInt(permission) },
    mention: `<@&${id}>`, toString: () => data.name || id };
}

/* ------------------------------------------------------------------ client */

class Client {
  constructor(options = {}) {
    this.options = { intents: [], ...options };
    this.handlers = new Map();
    this._interaction_replies = new Map();
    this.guilds = { cache: new Collection() };
    this.channels = { cache: new Collection() };
    this.users = { cache: new Collection() };
    this.user = null;
    this.readyAt = null;
    this._send = (payload) => {
      process.stdout.write(JSON.stringify(payload) + "\n");
    };
    this._send({ type: "hello" });
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
    process.stdin.on("end", () => process.exit(0));
  }

  _dispatch(event) {
    if (event.type === "ready") {
      this.user = _wireUser(event.user);
      this.users.cache.set(this.user.id, this.user);
      this.readyAt = new Date();
      this.guilds.cache.clear();
      this.channels.cache.clear();
      const guild = _wireGuild(this, event.guild || { id: event.guild_id, name: "Simulated Server",
        members: event.members || [], channels: event.channels || [], roles: event.roles || [] });
      this.guilds.cache.set(guild.id, guild);
      this.guilds.fetch = async (id) => {
        const found = this.guilds.cache.get(String(id));
        if (!found) throw new Error(`Unknown guild ${id}`);
        return found;
      };
      this.channels.fetch = async (id) => {
        const found = this.channels.cache.get(String(id));
        if (!found) throw new Error(`Unknown channel ${id}`);
        return found;
      };
      this.users.fetch = async (id) => {
        const found = this.users.cache.get(String(id));
        if (!found) throw new Error(`Unknown user ${id}`);
        return found;
      };
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
    if (event.type === "guild_cache") {
      const guild = this.guilds.cache.get(String(event.guild_id || ""));
      if (!guild) return;
      const members = (event.members || []).map((raw) => _wireMember(this, raw, guild)).filter(Boolean);
      guild.members.cache.clear();
      guild.memberCount = members.length;
      guild.roles.cache.clear();
      for (const role of event.roles || []) guild.roles.cache.set(String(role.id), _wireRole(role, guild));
      for (const member of members) {
        member.roles.cache.clear();
        for (const roleId of member.rolesData || []) {
          const role = guild.roles.cache.get(String(roleId));
          if (role) member.roles.cache.set(String(roleId), role);
        }
        guild.members.cache.set(String(member.id), member);
        this.users.cache.set(String(member.user.id), member.user);
      }
      for (const [id, channel] of guild.channels.cache) {
        this.channels.cache.delete(id);
      }
      guild.channels.cache.clear();
      for (const raw of event.channels || []) {
        const channel = _wireChannel(this, raw);
        channel.guild = guild;
        guild.channels.cache.set(channel.id, channel);
        this.channels.cache.set(channel.id, channel);
      }
      return;
    }
    if (event.type === "message") {
      this._rememberGuildMembers(event.guild_members, event.guild_id);
      const message = new Message(this, event);
      this._rememberMember(message.member);
      if (message.author) this.users.cache.set(String(message.author.id), message.author);
      this.emit("messageCreate", message);
      this.emit("message", message);
      return;
    }
    if (event.type === "interaction") {
      this._dispatchInteraction(event, ChatInputCommandInteraction);
      return;
    }
    if (event.type === "component") {
      this._dispatchInteraction(event, MessageComponentInteraction);
      return;
    }
    if (event.type === "modal_submit") {
      this._dispatchInteraction(event, ModalSubmitInteraction);
      return;
    }
  }

  _rememberGuildMembers(members, guildId) {
    const guild = this.guilds.cache.get(String(guildId || ""));
    if (!guild) return;
    for (const raw of members || []) {
      const member = _wireMember(this, raw, guild);
      if (!member) continue;
      const existed = guild.members.cache.has(String(member.id));
      guild.members.cache.set(String(member.id), member);
      this.users.cache.set(String(member.user.id), member.user);
      if (!existed) guild.memberCount += 1;
    }
  }

  _rememberMember(member) {
    if (!member) return;
    this.users.cache.set(String(member.user.id), member.user);
    const guild = member.guild;
    if (!guild) return;
    const existed = guild.members.cache.has(String(member.id));
    guild.members.cache.set(String(member.id), member);
    if (!existed) guild.memberCount += 1;
  }

  async _dispatchInteraction(event, InteractionClass) {
    this._rememberGuildMembers(event.guild_members, event.guild_id);
    const interaction = new InteractionClass(this, event);
    this._rememberMember(interaction.member);
    if (interaction.user) this.users.cache.set(String(interaction.user.id), interaction.user);
    if (interaction.channel && interaction.channel.id) {
      this.channels.cache.set(String(interaction.channel.id), interaction.channel);
    }
    const handlers = [...(this.handlers.get("interactionCreate") || [])];
    await Promise.all(handlers.map(async (handler) => {
      try { await handler(interaction); }
      catch (error) {
        this._send({ type: "error", interaction_id: interaction.id,
          message: String((error && error.stack) || error) });
      }
    }));
    this._send({ type: "interaction_complete", interaction_id: interaction.id,
      acknowledged: interaction.replied || interaction.deferred });
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
    const wrapped = (...args) => { this._off(name, wrapped); return handler(...args); };
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
    this.createdAt = new Date(this.createdTimestamp);
    this.member = data.member ? _wireMember(client, data.member, client.guilds.cache.get(String(data.guild_id || ""))) : null;
    this.guild = client.guilds.cache.get(String(data.guild_id || "")) || null;
    this.components = data.components || [];
    this.attachments = new Collection((data.attachments || []).map((attachment) => [String(attachment.id), attachment]));
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
    return this.client.channels.cache.get(String(this.channelId))
      || _wireChannel(this.client, { id: this.channelId });
  }

  async reply(payload) {
    this.client._send({ type: "send", channel_id: this.channelId, ..._normalizeSend(payload), reply_to: this.id });
    return { id: null };
  }

  get editable() { return false; }
}

class Interaction {
  constructor(client, data) {
    this.client = client;
    this.id = data.interaction_id;
    this.commandName = data.command_name;
    this.type = data.type === "interaction" ? 2 : (data.type || 0);
    this.user = _wireUser(data.user);
    this.member = _wireMember(client, data.member, client.guilds.cache.get(String(data.guild_id || "")));
    this.channelId = data.channel_id;
    this.channelData = data.channel || null;
    this.guildId = data.guild_id;
    this.guild = client.guilds.cache.get(String(data.guild_id || "")) || data.guild || null;
    this.permissions = _permissions(data.permissions || 0);
    this._appPermissions = _permissions(data.app_permissions || 0);
    this.message = data.message || null;
    this.locale = data.locale || "en-US";
    this.createdTimestamp = Date.now();
    this.createdAt = new Date(this.createdTimestamp);
    this._replied = false;
    this._deferred = false;
    this._thinking = false;
    this._ephemeral = false;
    this.options = {
      data: data.option_data || [],
      _map: data.options || {},
      get: (n) => this.options.data.find((item) => item.name === n) || null,
      getString: (n) => this.options._map[n] ?? null,
      getInteger: (n) => { const v = this.options._map[n]; return v === undefined ? null : Number(v); },
      getNumber: (n) => { const v = this.options._map[n]; return v === undefined ? null : Number(v); },
      getBoolean: (n) => { const v = this.options._map[n]; return v === undefined ? null : !!v; },
      getUser: (n) => _resolveUser(client, data.resolved && data.resolved.users && data.resolved.users[this.options._map[n]]),
      getChannel: (n) => _resolveChannel(client, data.resolved && data.resolved.channels && data.resolved.channels[this.options._map[n]]),
      getRole: (n) => _resolveRole(client, data.resolved && data.resolved.roles && data.resolved.roles[this.options._map[n]], this.guildId),
      getMember: (n) => _resolveMember(client, data.resolved && data.resolved.members && data.resolved.members[this.options._map[n]], this.guildId),
      getSubcommand: () => { throw new Error("No subcommand is selected"); },
      getSubcommandGroup: () => null,
      getFocused: () => null,
    };
  }

  get channel() {
    return this.client.channels.cache.get(String(this.channelId))
      || _wireChannel(this.client, this.channelData || { id: this.channelId });
  }

  get memberPermissions() { return this.channel.permissionsFor(this.member); }
  get appPermissions() {
    const bot = this.guild && this.guild.members.cache.get(String(this.client.user && this.client.user.id));
    return this.channel.permissionsFor(bot) || this._appPermissions;
  }

  async reply(payload) {
    if (this._replied || this._deferred) throw new Error("Interaction already acknowledged");
    this._replied = true;
    const message = _normalizeSend(payload);
    this._ephemeral = !!message.ephemeral;
    this.client._interaction_replies.set(this.id, { channel_id: this.channelId, ephemeral: this._ephemeral });
    this.client._send({ type: "interaction_reply", interaction_id: this.id,
      channel_id: this.channelId, ...message });
    return {};
  }

  async deferReply(options = {}) {
    if (this._replied || this._deferred) throw new Error("Interaction already acknowledged");
    this._deferred = true;
    this._thinking = true;
    this._ephemeral = !!options.ephemeral || !!(Number(options.flags) & 64);
    this.client._interaction_replies.set(this.id, { channel_id: this.channelId, ephemeral: this._ephemeral });
    this.client._send({ type: "interaction_defer", interaction_id: this.id,
      thinking: this._thinking, ephemeral: this._ephemeral });
    return {};
  }

  async editReply(payload) {
    if (!this._replied && !this._deferred) throw new Error("Interaction has not been acknowledged");
    const message = _normalizeSend(payload);
    this.client._send({ type: "interaction_edit", interaction_id: this.id,
      content: message.content, embeds: message.embeds, components: message.components });
    return {};
  }

  async deleteReply() {
    if (!this._replied && !this._deferred) throw new Error("Interaction has not been acknowledged");
    this.client._send({ type: "interaction_delete", interaction_id: this.id });
  }
  async followUp(payload) {
    if (!this._replied && !this._deferred) throw new Error("Interaction has not been acknowledged");
    const message = _normalizeSend(payload);
    if (this._ephemeral && message.ephemeral === undefined) message.ephemeral = true;
    this.client._send({ type: "interaction_reply", interaction_id: this.id,
      channel_id: this.channelId, followup: true, ...message });
    return {};
  }
  isRepliable() { return true; }
  inGuild() { return !!this.guildId; }
  inCachedGuild() { return !!this.guild; }
  isChatInputCommand() { return false; }
  isCommand() { return false; }
  isAutocomplete() { return false; }
  isMessageComponent() { return false; }
  isModalSubmit() { return false; }
  isButton() { return false; }
  isStringSelectMenu() { return false; }
  isUserSelectMenu() { return false; }
  isRoleSelectMenu() { return false; }
  isMentionableSelectMenu() { return false; }
  isChannelSelectMenu() { return false; }
  isUserContextMenuCommand() { return false; }
  isContextMenuCommand() { return false; }
  isApplicationCommand() { return false; }
  isPrimaryEntryPointCommand() { return false; }
  get replied() { return this._replied; }
  get deferred() { return this._deferred; }
  async showModal(modal) {
    if (this._replied || this._deferred) throw new Error("Interaction already acknowledged");
    this._replied = true;
    this.client._send({ type: "interaction_modal", interaction_id: this.id,
      modal: modal.toJSON ? modal.toJSON() : modal });
  }
  get webhook() { return { send: (payload) => this.followUp(payload) }; }
}

class ChatInputCommandInteraction extends Interaction {
  constructor(client, data) {
    super(client, data);
    this.commandName = data.command_name;
    this.options = {
      _map: data.options || {},
      getString: (name) => this.options._map[name] ?? null,
      getInteger: (name) => { const value = this.options._map[name]; return value === undefined ? null : Number(value); },
      getNumber: (name) => { const value = this.options._map[name]; return value === undefined ? null : Number(value); },
      getBoolean: (name) => { const value = this.options._map[name]; return value === undefined ? null : !!value; },
      getUser: (name) => _resolveUser(client, data.resolved && data.resolved.users && data.resolved.users[this.options._map[name]]),
      getChannel: (name) => _resolveChannel(client, data.resolved && data.resolved.channels && data.resolved.channels[this.options._map[name]]),
      getRole: (name) => _resolveRole(client, data.resolved && data.resolved.roles && data.resolved.roles[this.options._map[name]], this.guildId),
      getMember: (name) => _resolveMember(client, data.resolved && data.resolved.members && data.resolved.members[this.options._map[name]], this.guildId),
      data: data.option_data || [],
      get: (name) => this.options.data.find((item) => item.name === name) || null,
      getSubcommand: () => { throw new Error("No subcommand is selected"); },
      getSubcommandGroup: () => null,
      getFocused: () => null,
    };
  }
  isChatInputCommand() { return true; }
  isCommand() { return true; }
  isApplicationCommand() { return true; }
}

class MessageComponentInteraction extends Interaction {
  constructor(client, data) {
    super(client, data);
    this.type = 3;
    this.customId = data.custom_id;
    this.componentType = data.component_type;
    this.values = data.values || [];
    const source = data.message;
    this.message = source ? new Message(client, { ...source,
      message_id: source.id || source.message_id, channel_id: source.channel_id || data.channel_id,
      guild_id: data.guild_id }) : null;
    if (this.message) this.message.components = source.components || [];
    this.data = { custom_id: this.customId, component_type: this.componentType, values: this.values };
  }
  isCommand() { return false; }
  isApplicationCommand() { return false; }
  isMessageComponent() { return true; }
  isButton() { return this.componentType === 2; }
  isStringSelectMenu() { return this.componentType === 3; }
  isUserSelectMenu() { return this.componentType === 5; }
  isRoleSelectMenu() { return this.componentType === 6; }
  isMentionableSelectMenu() { return this.componentType === 7; }
  isChannelSelectMenu() { return this.componentType === 8; }
  async update(payload) {
    if (this._replied || this._deferred) throw new Error("Interaction already acknowledged");
    this._replied = true;
    const message = _normalizeSend(payload);
    this.client._send({ type: "interaction_update", interaction_id: this.id,
      content: message.content, embeds: message.embeds, components: message.components });
    return {};
  }
  async deferUpdate() {
    if (this._replied || this._deferred) throw new Error("Interaction already acknowledged");
    this._deferred = true;
    this._thinking = false;
    this.client._send({ type: "interaction_defer", interaction_id: this.id, thinking: false });
  }
}

class ModalSubmitInteraction extends Interaction {
  constructor(client, data) {
    super(client, data);
    this.type = 5;
    this.customId = data.custom_id;
    this.data = { custom_id: this.customId, components: data.components || [] };
    const source = data.message;
    this.message = source ? new Message(client, { ...source,
      message_id: source.id || source.message_id, channel_id: source.channel_id || data.channel_id,
      guild_id: data.guild_id }) : null;
    this.fields = {
      fields: new Map(Object.entries(data.values || {}).map(([customId, value]) =>
        [customId, { customId, value: String(value) }])),
      getTextInputValue(customId) {
        const field = this.fields.get(customId);
        if (!field) throw new Error(`Missing modal field ${customId}`);
        return field.value;
      },
    };
  }
  isCommand() { return false; }
  isApplicationCommand() { return false; }
  isModalSubmit() { return true; }
}

class TextInputBuilder {
  constructor() { this.data = { type: 4, style: 1, required: true }; }
  setCustomId(value) { this.data.custom_id = value; return this; }
  setLabel(value) { this.data.label = value; return this; }
  setStyle(value) { this.data.style = typeof value === "number" ? value : ({ Short: 1, Paragraph: 2 }[value] || 1); return this; }
  setPlaceholder(value) { this.data.placeholder = value; return this; }
  setValue(value) { this.data.value = value; return this; }
  setRequired(value = true) { this.data.required = !!value; return this; }
  setMinLength(value) { this.data.min_length = value; return this; }
  setMaxLength(value) { this.data.max_length = value; return this; }
  toJSON() { return this.data; }
}

class ModalBuilder {
  constructor() { this.data = { custom_id: "", title: "", components: [] }; }
  setCustomId(value) { this.data.custom_id = value; return this; }
  setTitle(value) { this.data.title = value; return this; }
  addComponents(...items) { this.data.components.push(...items.flat().map((item) => item.toJSON ? item.toJSON() : item)); return this; }
  toJSON() { return this.data; }
}

function _normalizeSend(payload) {
  if (payload === null || payload === undefined) return { content: "" };
  if (typeof payload === "string") return { content: payload };
  const out = {};
  if (payload.content !== undefined) out.content = payload.content;
  if (payload.embeds) out.embeds = payload.embeds.map((e) => (e.toJSON ? e.toJSON() : e));
  if (payload.embed) out.embeds = [payload.embed.toJSON ? payload.embed.toJSON() : payload.embed];
  if (payload.ephemeral) out.ephemeral = true;
  if (payload.flags !== undefined) out.flags = payload.flags;
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
  CreateInstantInvite: 1n << 0n, KickMembers: 1n << 1n, BanMembers: 1n << 2n,
  Administrator: 1n << 3n, ManageChannels: 1n << 4n, ManageGuild: 1n << 5n,
  AddReactions: 1n << 6n, ViewAuditLog: 1n << 7n, PrioritySpeaker: 1n << 8n,
  Stream: 1n << 9n, ViewChannel: 1n << 10n, SendMessages: 1n << 11n,
  SendTTSMessages: 1n << 12n, ManageMessages: 1n << 13n, EmbedLinks: 1n << 14n,
  AttachFiles: 1n << 15n, ReadMessageHistory: 1n << 16n, MentionEveryone: 1n << 17n,
  UseExternalEmojis: 1n << 18n, ViewGuildInsights: 1n << 19n, Connect: 1n << 20n,
  Speak: 1n << 21n, MuteMembers: 1n << 22n, DeafenMembers: 1n << 23n,
  MoveMembers: 1n << 24n, UseVAD: 1n << 25n, ChangeNickname: 1n << 26n,
  ManageNicknames: 1n << 27n, ManageRoles: 1n << 28n, ManageWebhooks: 1n << 29n,
  ManageGuildExpressions: 1n << 30n, UseApplicationCommands: 1n << 31n,
  RequestToSpeak: 1n << 32n, ManageEvents: 1n << 33n, ManageThreads: 1n << 34n,
  CreatePublicThreads: 1n << 35n, CreatePrivateThreads: 1n << 36n,
  UseExternalStickers: 1n << 37n, SendMessagesInThreads: 1n << 38n,
  UseEmbeddedActivities: 1n << 39n, ModerateMembers: 1n << 40n,
  ViewCreatorMonetizationAnalytics: 1n << 41n, UseSoundboard: 1n << 42n,
  CreateGuildExpressions: 1n << 43n, CreateEvents: 1n << 44n,
  UseExternalSounds: 1n << 45n, SendVoiceMessages: 1n << 46n,
};

const Partials = { Message: 0, Channel: 1, Reaction: 2, User: 3 };

module.exports = {
  Client, Embed: EmbedBuilder, EmbedBuilder, SlashCommandBuilder,
  ButtonBuilder, ActionRowBuilder, ModalBuilder, TextInputBuilder, Collection, REST, Routes,
  TextInputStyle: { Short: 1, Paragraph: 2 },
  ComponentType: { ActionRow: 1, Button: 2, StringSelect: 3, TextInput: 4,
    UserSelect: 5, RoleSelect: 6, MentionableSelect: 7, ChannelSelect: 8 },
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
