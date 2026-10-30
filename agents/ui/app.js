// DesignAround workbench: chat with the assistant (isolated Pi runtime).
(function () {
  const $ = (id) => document.getElementById(id);
  const { _t, _tf } = window; // i18n.js (NFR-I18N-01)
  window.daTranslateDom(document.body);
  const state = { session: null, source: null, current: null, tools: {}, attachments: [], busy: false };

  const TOOL_LABELS = {
    status: _t("账户状态"), case_list: _t("案卷列表"), case_create: _t("新建案卷"), case_show: _t("查看案卷"), case_set: _t("更新案卷"), case_decide: _t("记录决定"),
    templates: _t("模板"), model_from_template: _t("模板建模"), model_import_cad: _t("导入 CAD"), photos_add: _t("添加照片"), model_from_photos: _t("CAD Agent 建模"),
    cad_program: _t("运行 CAD 程序"), cad_tweak: _t("参数微调"), cad_params: _t("参数"), cad_versions: _t("版本"), cad_sheet: _t("视图"), cad_compare: _t("版本对比"), cad_claim: _t("保护范围"),
    screen: _t("注册库检索"), prior_art_add: _t("添加在先设计"), prior_art_rank: _t("剪影排序"), comparison_sheet: _t("比对图"), observe: _t("观察者评分"), evaluate: _t("风险评估"),
    design_around: _t("规避改型"), originality: _t("原创性检查"), dfm: _t("可制造性"), viewpack: _t("专利视图"), filing_pack: _t("申请材料包"), quote: _t("报价"),
    disclose: _t("记录公开"), timeline: _t("期限"), proto: _t("打样包"), evidence: _t("证据"), gate_create: _t("专家闸门"), credits: _t("积分"), billing_checkout: _t("付款链接"),
  };

  async function api(path, opts = {}) {
    const res = await fetch(path, { credentials: "same-origin", ...opts, headers: { "Content-Type": "application/json", "X-DA-Lang": window.DA_LANG, ...(opts.headers || {}) } });
    if (!res.ok) throw new Error((await res.text()).slice(0, 300) || res.statusText);
    const type = res.headers.get("content-type") || "";
    return type.includes("json") ? res.json() : res.text();
  }

  function el(tag, cls, text) {
    const e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text !== undefined) e.textContent = text;
    return e;
  }

  function icon(name) {
    const t = document.createElement("template");
    t.innerHTML = (window.DA_ICONS || {})[name] || "";
    return t.content.firstChild || document.createTextNode("");
  }
  function hydrateIcons(root) {
    for (const i of (root || document).querySelectorAll("i[data-icon]")) i.replaceWith(icon(i.dataset.icon));
  }
  hydrateIcons(document);
  const fmt = (n) => Number(n || 0).toLocaleString("en-US");

  function fileUrl(path) {
    return "/api/file?path=" + encodeURIComponent(path);
  }
  function toast(text, kind = "info") {
    const t = el("div", "toast " + kind, text);
    $("toasts").appendChild(t);
    requestAnimationFrame(() => t.classList.add("in"));
    setTimeout(() => {
      t.classList.remove("in");
      setTimeout(() => t.remove(), 300);
    }, kind === "error" ? 7000 : 4500);
  }

  function scrollDown() {
    const box = $("messages");
    box.scrollTop = box.scrollHeight;
  }

  // ------------------------------------------------------------- rendering

  function addUser(text, files) {
    $("welcome")?.remove();
    const row = el("div", "msg user");
    row.appendChild(el("div", "bubble", text));
    if (files && files.length) {
      const strip = el("div", "thumbs");
      for (const f of files) {
        if (/\.(png|jpe?g|webp)$/i.test(f)) {
          const img = el("img");
          img.src = fileUrl(f);
          img.addEventListener("click", () => lightbox(f));
          strip.appendChild(img);
        } else strip.appendChild(el("span", "file-chip", f.split("/").pop()));
      }
      row.appendChild(strip);
    }
    $("messages").appendChild(row);
    scrollDown();
  }

  function assistantBlock() {
    $("welcome")?.remove();
    const row = el("div", "msg assistant");
    const body = el("div", "bubble md");
    row.appendChild(body);
    $("messages").appendChild(row);
    return { row, body, text: "" };
  }

  function setAssistantText(block, text) {
    block.text = text;
    block.body.innerHTML = window.renderMarkdown(text);
    if (!text) block.body.classList.add("empty");
    else block.body.classList.remove("empty");
  }

  function toolCard(id, name, args) {
    const card = el("details", "tool running");
    const summary = el("summary");
    const st = el("span", "tool-status");
    st.appendChild(icon("Loading03"));
    summary.appendChild(st);
    summary.appendChild(el("span", "tool-name", TOOL_LABELS[name] || name));
    const brief = el("span", "tool-args", briefArgs(args));
    summary.appendChild(brief);
    card.appendChild(summary);
    const log = el("pre", "tool-log");
    card.appendChild(log);
    const imgs = el("div", "tool-images");
    card.appendChild(imgs);
    $("messages").appendChild(card);
    state.tools[id] = { card, log, imgs, status: summary.firstChild };
    scrollDown();
    return state.tools[id];
  }

  function briefArgs(args) {
    if (!args) return "";
    const keep = ["case_id", "version", "prior", "template", "office", "name", "number", "a", "b"];
    return keep.filter((k) => args[k] !== undefined).map((k) => String(args[k])).join(" · ");
  }

  function finishTool(ev) {
    const t = state.tools[ev.id] || toolCard(ev.id, ev.name, {});
    t.card.classList.remove("running");
    t.card.classList.add(ev.declined ? "declined" : ev.ok ? "ok" : "failed");
    t.status.replaceChildren(icon(ev.declined ? "MinusSignCircle" : ev.ok ? "CheckmarkCircle02" : "AlertCircle"));
    if (ev.text) t.log.textContent += (t.log.textContent ? "\n" : "") + ev.text;
    if (ev.cost && (ev.cost.charged || ev.cost.estimated)) t.card.querySelector("summary").appendChild(el("span", "tool-cost", _tf("{0} 积分", fmt(ev.cost.charged ?? 0))));
    for (const path of ev.images || []) {
      const img = el("img");
      img.src = fileUrl(path);
      img.loading = "lazy";
      img.addEventListener("click", () => lightbox(path));
      t.imgs.appendChild(img);
    }
    if ((ev.images || []).length) t.card.open = true;
    scrollDown();
  }

  function showError(text) {
    const box = el("div", "error");
    box.appendChild(icon("AlertCircle"));
    box.appendChild(el("span", "", text));
    $("messages").appendChild(box);
    scrollDown();
  }

  function lightbox(path) {
    const box = $("lightbox");
    box.querySelector("img").src = fileUrl(path);
    box.classList.remove("hidden");
  }
  $("lightbox").addEventListener("click", () => $("lightbox").classList.add("hidden"));

  function setMeter(spent) {
    state.spent = spent;
    const cap = state.budget || 3000;
    $("meter-text").textContent = _tf("{0} / {1} 积分", fmt(spent), fmt(cap));
    $("meter-bar").style.width = Math.min(100, (spent / cap) * 100) + "%";
  }

  function setBusy(on) {
    state.busy = on;
    $("busy").classList.toggle("hidden", !on);
    $("abort").classList.toggle("hidden", !on);
  }

  function renderTranscript(items) {
    $("messages").innerHTML = "";
    state.tools = {};
    for (const item of items) {
      if (item.role === "user") addUser(item.text.replace(/\n\n\[Attached files \(absolute paths\): (.*)\]$/s, ""), (item.text.match(/\[Attached files \(absolute paths\): (.*)\]$/s) || [, ""])[1].split(", ").filter(Boolean));
      else if (item.role === "assistant") {
        if (item.text) setAssistantText(assistantBlock(), item.text);
        for (const t of item.tools || []) toolCard(t.id, t.name, t.args);
        if (item.error) showError(item.error);
      } else if (item.role === "tool") finishTool({ id: item.id, name: item.name, ok: item.ok, declined: item.declined, images: item.images, text: "" });
    }
    if (!items.length) $("messages").innerHTML = "";
  }

  // ------------------------------------------------------------- sessions

  async function loadSessions() {
    const rows = await api("/api/sessions");
    rows.sort((a, b) => new Date(b.modified) - new Date(a.modified));
    const list = $("sessions");
    list.innerHTML = "";
    if (!rows.length) list.appendChild(el("li", "empty", _t("还没有对话")));
    for (const s of rows.slice(0, 40)) {
      const li = el("li", s.id === state.session ? "active" : "");
      li.appendChild(el("span", "txt", s.name || s.first || _t("（空对话）")));
      li.title = new Date(s.modified).toLocaleString();
      li.addEventListener("click", () => openSession(s.id));
      list.appendChild(li);
    }
  }

  async function loadCases() {
    try {
      const rows = await api("/api/cases");
      const list = $("cases");
      list.innerHTML = "";
      if (!rows.length) list.appendChild(el("li", "empty", _t("新案卷会出现在这里")));
      for (const c of rows.slice(0, 40)) {
        const li = el("li", state.caseId === c.id ? "active" : "");
        if (c.thumb) {
          const img = el("img", "thumb");
          img.alt = "";
          img.src = fileUrl(c.thumb);
          img.onerror = () => img.remove();
          li.appendChild(img);
        }
        const txt = el("span", "txt", c.name || c.id);
        txt.appendChild(el("span", "sub", [(c.jurisdictions || []).join(" · "), c.versions != null ? _tf("{0} 个版本", c.versions) : ""].filter(Boolean).join("  ")));
        li.appendChild(txt);
        li.title = c.id;
        li.addEventListener("click", () => showCase(c.id));
        list.appendChild(li);
      }
    } catch (e) {
      /* engine not ready yet */
    }
  }

  const RISK_ZH = { low: _t("低"), medium: _t("中"), high: _t("高") };
  const LONG_TOOLS = { design_around: _t("规避改型"), design_build: _t("3D 建模"), design_concepts: _t("设计方向"), design_revise: _t("方向修改"), screen: _t("检索"), batch_screen: _t("批量检索"), model_from_photos: _t("照片建模"), observe: _t("观察者评分") };
  async function showCase(id) {
    const data = await api("/api/cases/" + encodeURIComponent(id));
    state.caseId = id;
    $("panel").classList.remove("hidden");
    $("panel-title").textContent = data.case.name || id;
    $("panel-sub").textContent = [data.case.article_zh || data.case.article, (data.case.jurisdictions || []).join(" · "), (data.case.platforms || []).join(", ")].filter(Boolean).join("  ·  ");
    const body = $("panel-body");
    body.innerHTML = "";
    const scored = data.versions.filter((v) => typeof v.risk === "number" && !v.risk_provisional);
    const best = scored.length ? scored.reduce((a, b) => (b.risk < a.risk ? b : a)) : null;
    const facts = el("div", "facts");
    const fact = (k, v, unit) => {
      const d = el("div");
      d.appendChild(el("div", "k", k));
      const vv = el("div", "v", v);
      if (unit) vv.appendChild(el("small", "", unit));
      d.appendChild(vv);
      facts.appendChild(d);
    };
    fact(_t("在先设计"), String(data.prior_art.length), _t("件"));
    fact(_t("版本"), String(data.versions.length), _t("个"));
    fact(_t("最低风险"), best ? best.risk.toFixed(2) : "—", best ? best.id : "");
    if (data.card) body.appendChild(decisionCard(id, data.card));
    body.appendChild(facts);

    const concepts = data.concepts || [];
    if (concepts.length) {
      const ch = el("h4");
      ch.appendChild(el("span", "", _t("设计方向（2D 概念图）")));
      ch.appendChild(el("span", "", data.case.chosen_concept ? _tf("已选 {0}", data.case.chosen_concept) : _t("未选择")));
      body.appendChild(ch);
      const grid = el("div", "concepts");
      for (const c of concepts) {
        const card = el("div", "concept" + (c.chosen ? " chosen" : ""));
        const img = el("img");
        img.alt = c.title || c.id;
        img.src = fileUrl(c.image);
        img.addEventListener("click", () => lightbox(c.image));
        card.appendChild(img);
        const meta = el("div", "cm");
        const head = el("div", "ct");
        head.appendChild(el("span", "num", c.id));
        head.appendChild(document.createTextNode(c.title || ""));
        meta.appendChild(head);
        const review = c.review || {};
        const status = c.chosen ? _t("已选定") : review.pass ? _tf("审核通过 · 契合 {0}/10", review.fit) : review.fit != null ? _tf("未通过：{0}", (review.issues || [])[0] || _t("不符合要求")) : _t("待审核");
        const cs = el("div", "cs " + (c.chosen ? "ok" : review.pass ? "ok" : review.fit != null ? "bad" : ""), status);
        cs.title = [c.idea, ...(review.issues || [])].filter(Boolean).join("\n");
        meta.appendChild(cs);
        if (!c.chosen) {
          const pick = el("button", "link-btn", _t("选这个方向"));
          pick.addEventListener("click", () => sendText(_tf("我选 {0}（{1}）这个方向，继续做 3D 模型。", c.id, c.title || "")));
          meta.appendChild(pick);
        }
        card.appendChild(meta);
        grid.appendChild(card);
      }
      body.appendChild(grid);
    }

    const vh = el("h4");
    vh.appendChild(el("span", "", _t("版本")));
    vh.appendChild(el("span", "", _t("风险")));
    body.appendChild(vh);
    if (!data.versions.length) body.appendChild(el("div", "empty-row", concepts.length ? _t("选定一个方向后，CAD Agent 会把它建成 3D 模型。") : _t("还没有 3D 模型。描述你的产品，助手会先出几个设计方向。")));
    for (const v of data.versions) {
      const isOriginal = v.id === data.case.original;
      const card = el("div", "version clickable" + (best && v.id === best.id && !isOriginal ? " best" : ""));
      card.title = _t("查看这个版本");
      const img = el("img");
      img.alt = "";
      const views = v.path + "/views/perspective.";
      img.src = fileUrl(views + "render.png");
      img.onerror = () => {
        img.onerror = null;
        img.src = fileUrl(views + "lineart.png");
      };
      card.appendChild(img);
      const mid = el("div");
      const vt = el("div", "vt");
      vt.appendChild(el("span", "num", v.id));
      const isDraft = v.kind === "draft";
      vt.appendChild(document.createTextNode(isOriginal ? _t("原始设计") : isDraft ? _t("3D 草稿") : v.kind === "original" ? _t("旧的原始设计") : _t("候选方案")));
      mid.appendChild(vt);
      const draftNote = isDraft ? _tf("审核未通过（一致度 {0}/10），可以让助手重建，或你确认后采用", (v.review && v.review.match) ?? "?") : "";
      mid.appendChild(el("div", "vd", isOriginal ? (v.concept ? _tf("由方向 {0} 建模，作为比较基准", v.concept) : _t("你的产品，作为比较基准")) : draftNote || v.label || ""));
      card.appendChild(mid);
      card.addEventListener("click", () => showVersion(id, v.id));
      const provisional = !!v.risk_provisional;
      const lvl = provisional ? "none" : v.risk_level || (typeof v.risk === "number" ? (v.risk >= 0.6 ? "high" : v.risk >= 0.35 ? "medium" : "low") : "none");
      const risk = el("div", "risk " + lvl);
      risk.appendChild(el("div", "rn", typeof v.risk === "number" && !provisional ? v.risk.toFixed(2) : "—"));
      risk.appendChild(el("div", "rl", provisional ? _t("待观察者评分") : lvl === "none" ? _t("未评估") : _tf("{0}风险{1}", RISK_ZH[lvl] || lvl, v.qualified ? _t(" · 合格") : "")));
      card.appendChild(risk);
      body.appendChild(card);
    }
    const ph = el("h4");
    ph.appendChild(el("span", "", _t("在先设计")));
    ph.appendChild(el("span", "", _t("公告日")));
    body.appendChild(ph);
    for (const p of data.prior_art.slice(0, 30)) {
      const row = el("div", "prior");
      const fig = p.figures && p.figures[0] && data.case.root ? data.case.root + "/" + p.figures[0] : null;
      if (fig) {
        const img = el("img");
        img.alt = "";
        img.src = fileUrl(fig);
        img.addEventListener("click", () => lightbox(fig));
        row.appendChild(img);
      } else row.appendChild(el("span", "ph"));
      const mid = el("div");
      mid.appendChild(el("div", "pn", p.number));
      mid.appendChild(el("div", "pt", p.title || ""));
      row.appendChild(mid);
      row.appendChild(el("div", "pd", p.date || ""));
      body.appendChild(row);
    }
    body.appendChild(deadlinesBlock(id));
    body.appendChild(deliverablesBlock(id));
    body.appendChild(sharesBlock(id));
    body.appendChild(monitorBlock(id, Boolean((data.prior_art || []).length || (data.card && data.card.screened_at))));
    const ev = data.case.evidence || {};
    const chain = el("div", "chain");
    chain.appendChild(icon(ev.chain_ok ? "CheckmarkCircle02" : "AlertCircle"));
    chain.firstChild.classList.add(ev.chain_ok ? "ok" : "bad");
    chain.appendChild(el("span", "", _tf("证据链 {0} 条记录{1}", ev.events ?? 0, ev.chain_ok ? _t("，校验通过") : _t("，校验失败"))));
    body.appendChild(chain);
    const actions = el("div", "panel-actions");
    const ask = el("button", "btn", _t("在对话里继续这个案卷"));
    ask.addEventListener("click", () => sendText(_tf("继续案卷 {0}，告诉我现在进行到哪一步、下一步做什么。", id)));
    actions.appendChild(ask);
    body.appendChild(actions);
    loadCases();
  }
  // ------------------------------------------------------- decision card (FR-UX-02/03)

  function decisionCard(caseId, card) {
    const box = el("section", "dcard " + (card.verdict || "pending"));
    const steps = el("ol", "dstages");
    (card.stages || []).forEach((name, i) => steps.appendChild(el("li", i < card.stage_index ? "done" : i === card.stage_index ? "now" : "", name)));
    box.appendChild(steps);
    box.appendChild(el("div", "dhead", card.headline));
    if (card.summary) box.appendChild(el("p", "dsum", card.summary));
    if ((card.reasons || []).length) {
      const ul = el("ul", "dreasons");
      for (const r of card.reasons.slice(0, 4)) ul.appendChild(el("li", "", r));
      box.appendChild(ul);
    }
    if ((card.next || []).length) {
      const next = el("div", "dnext");
      for (const s of card.next) {
        const b = el("button", "dstep");
        b.appendChild(el("span", "dl", s.label));
        const cost = s.free ? _t("免费") : s.credits != null ? _tf("约 {0} 积分 · ${1}", fmt(s.credits), Number(s.usd || 0).toFixed(2)) : "";
        b.appendChild(el("span", "dc", [cost, s.minutes ? _tf("{0} 分钟", s.minutes) : ""].filter(Boolean).join(" · ")));
        b.title = s.command || "";
        b.addEventListener("click", () => sendText(_tf("案卷 {0}：{1}", caseId, s.prompt)));
        next.appendChild(b);
      }
      box.appendChild(next);
    }
    if (card.options) {
      const t = el("table", "doptions");
      const hr = el("tr");
      for (const h of [_t("选择"), _t("风险"), _t("平台费"), _t("申请费（估）"), _t("时间")]) hr.appendChild(el("th", "", h));
      t.appendChild(hr);
      for (const o of card.options) {
        const tr = el("tr");
        tr.title = o.note || "";
        for (const v of [o.label, o.risk, o.platform_credits ? _tf("{0} 积分", fmt(o.platform_credits)) : "0", o.filing_usd_estimate ? `$${fmt(o.filing_usd_estimate)}` : "—", o.minutes ? _tf("{0} 分钟", o.minutes) : "—"]) tr.appendChild(el("td", "", v));
        t.appendChild(tr);
      }
      box.appendChild(t);
    }
    if (card.managed) box.appendChild(el("p", "dmanaged", _tf("交给托管：平台费 ${0} + 代理费约 ${1}（直付代理师），官费另计", card.managed.platform_usd, card.managed.practitioner_usd_est)));
    const tools = el("div", "dtools");
    const rep = el("button", "link-btn", _t("生成风险报告 PDF"));
    rep.addEventListener("click", async () => {
      rep.disabled = true;
      rep.textContent = _t("正在生成…");
      try {
        const r = await api(`/api/cases/${encodeURIComponent(caseId)}/report`, { method: "POST", body: "{}" });
        toast(_t("风险报告已生成"), "ok");
        window.open(fileUrl(r.path), "_blank");
        showCase(caseId);
      } catch (e) {
        toast(e.message, "error");
      } finally {
        rep.disabled = false;
        rep.textContent = _t("生成风险报告 PDF");
      }
    });
    tools.appendChild(rep);
    box.appendChild(tools);
    box.appendChild(el("p", "dwording", card.wording));
    return box;
  }

  // ---------------------------------------------------- deliverables (T2-26)

  const KIND_ZH = { report: _t("风险报告"), filing_package: _t("申请材料包"), filing_viewpack: _t("专利视图包"), proto: _t("打样包"), evidence: _t("证据档案"), baseline: _t("提交基线"), compare: _t("版本对比图"), defense: _t("应诉材料") };
  function deliverablesBlock(caseId) {
    const wrap = el("div", "deliverables");
    const h = el("h4");
    h.appendChild(el("span", "", _t("交付物")));
    h.appendChild(el("span", "", ""));
    wrap.appendChild(h);
    const list = el("div", "dlist");
    list.appendChild(el("div", "empty-row", _t("正在读取…")));
    wrap.appendChild(list);
    api(`/api/cases/${encodeURIComponent(caseId)}/files`).then((rows) => {
      list.innerHTML = "";
      h.lastChild.textContent = rows.length ? _tf("{0} 项", rows.length) : "";
      if (!rows.length) list.appendChild(el("div", "empty-row", _t("还没有生成文件。报告、视图包、申请材料和打样包会出现在这里。")));
      for (const r of rows.slice(0, 30)) {
        const row = el("div", "drow");
        row.appendChild(el("span", "dk", KIND_ZH[r.kind] || r.kind));
        row.appendChild(el("span", "dt", r.title));
        row.appendChild(el("span", "ds", _tf("{0} 个文件 · {1} MB", r.files, (r.bytes / 1e6).toFixed(1))));
        const acts = el("span", "da");
        const target = r.open || r.path;
        if (target && /\.(pdf|png|jpe?g)$/i.test(target)) {
          const o = el("button", "link-btn", _t("打开"));
          o.addEventListener("click", () => window.open(fileUrl(target), "_blank"));
          acts.appendChild(o);
        } else if (target && /\.(zip|step|stp|stl|3mf|glb|docx|csv|json|md)$/i.test(target)) {
          const d = el("a", "link-btn", _t("下载"));
          d.href = fileUrl(target);
          acts.appendChild(d);
        }
        const show = el("button", "link-btn", _t("在文件夹中显示"));
        show.addEventListener("click", () => api("/api/reveal", { method: "POST", body: JSON.stringify({ path: r.path }) }).catch((e) => toast(e.message, "error")));
        acts.appendChild(show);
        row.appendChild(acts);
        list.appendChild(row);
      }
    }).catch(() => {
      list.innerHTML = "";
      list.appendChild(el("div", "empty-row", _t("读取交付物失败。")));
    });
    return wrap;
  }

  // ------------------------------------------------- deadline calendar (FR-FILE-12)

  function deadlinesBlock(caseId) {
    const wrap = el("div", "deadlines hidden");
    const h = el("h4");
    h.appendChild(el("span", "", _t("期限")));
    const ics = el("a", "link-btn h-act", _t("下载日历（.ics）"));
    h.appendChild(ics);
    wrap.appendChild(h);
    const list = el("div", "dllist");
    wrap.appendChild(list);
    api(`/api/cases/${encodeURIComponent(caseId)}/deadlines`).then((d) => {
      const rows = (d.deadlines || []).filter((r) => r.days_left >= 0);
      if (!rows.length) return;
      wrap.classList.remove("hidden");
      ics.href = fileUrl(d.ics);
      ics.setAttribute("download", "deadlines.ics");
      for (const r of rows.slice(0, 6)) {
        const row = el("div", "dlrow" + (r.hard && r.days_left <= 30 ? " soon" : "") + (r.hard ? "" : " info"));
        row.appendChild(el("span", "dld", r.due));
        row.appendChild(el("span", "dlt", `[${r.office}] ${window.DA_LANG === "en" ? r.title_en : r.title_zh}`));
        row.appendChild(el("span", "dll", r.hard ? _tf("还有 {0} 天", r.days_left) : _t("里程碑")));
        row.title = _tf("{0}\n依据：{1}", (window.DA_LANG === "en" ? r.note_en : r.note_zh) || "", r.basis);
        list.appendChild(row);
      }
      list.appendChild(el("div", "note", _t("期限以官方通知书为准，请执业代理人确认。日历文件里每个期限提前 30、14、7、1 天提醒。")));
    }).catch(() => {});
    return wrap;
  }

  // ------------------------------------------------- monitoring (FR-MON-01)

  function monitorBlock(caseId, hasScreen) {
    const wrap = el("div", "monitor");
    const h = el("h4");
    h.appendChild(el("span", "", _t("监控新公开的外观")));
    const act = el("button", "link-btn h-act", "");
    h.appendChild(act);
    wrap.appendChild(h);
    const line = el("div", "note", "");
    wrap.appendChild(line);
    const render = (m) => {
      if (m) {
        line.textContent = _tf("每 {0} 天按上次的条件复查一次（每次最多 {1} 积分），新命中推送到群机器人或系统通知。上次：{2}；下次：{3}。", m.every_days, m.max_credits, m.last_run ? day(m.last_run) + _t("（") + (m.last_status === "ok" ? _t("完成") : m.last_status) + _t("）") : _t("还没运行"), day(m.next_run));
        act.textContent = _t("停止监控");
      } else {
        line.textContent = hasScreen ? _t("开启后，每 30 天按这次检索的条件复查，新公开的相似外观会推送给你（快速检索每次 50 积分，每月前 5 次免费）。打开工作台时执行到期的复查。") : _t("先做一次检索，之后可以开启定期复查。");
        act.textContent = hasScreen ? _t("开启（每 30 天，每次最多 50 积分）") : "";
      }
      act.disabled = !hasScreen && !m;
      act.onclick = async () => {
        try {
          if (m) {
            await api(`/api/cases/${encodeURIComponent(caseId)}/monitor`, { method: "DELETE" });
            toast(_t("已停止监控"));
            render(null);
          } else {
            const d = await api(`/api/cases/${encodeURIComponent(caseId)}/monitor`, { method: "PUT", body: JSON.stringify({ every: 30, max_credits: 50 }) });
            toast(_t("已开启监控"));
            render({ ...d, next_run: d.next_run, last_run: d.last_run, last_status: d.last_status });
          }
        } catch (err) {
          toast(errText(err), "error");
        }
      };
    };
    api(`/api/cases/${encodeURIComponent(caseId)}/monitor`).then((d) => render(d.monitor)).catch(() => render(null));
    return wrap;
  }

  // ------------------------------------------------- share links (FR-CASE-09)

  const PURPOSE_ZH = { factory: _t("打样包"), agent: _t("视图和申请材料"), other: _t("报告和视图") };
  const SHARE_STATUS_ZH = { active: _t("有效"), revoked: _t("已撤回"), expired: _t("已过期"), draft: _t("未完成") };
  const day = (ts) => (ts ? new Date(ts * 1000).toLocaleString(window.DA_LANG === "en" ? "en-US" : "zh-CN", { month: "numeric", day: "numeric", hour: "2-digit", minute: "2-digit" }) : "-");
  function errText(err) {
    try {
      return JSON.parse(err.message).message || err.message;
    } catch {
      return err.message;
    }
  }
  async function copyText(text) {
    try {
      await navigator.clipboard.writeText(text);
      toast(_t("链接已复制"));
    } catch {
      const ta = el("textarea");
      ta.value = text;
      document.body.appendChild(ta);
      ta.select();
      const ok = document.execCommand && document.execCommand("copy");
      ta.remove();
      toast(ok ? _t("链接已复制") : _t("复制失败，请手动选中链接复制"), ok ? "info" : "error");
    }
  }

  function sharesBlock(caseId) {
    const wrap = el("div", "shares");
    const h = el("h4");
    h.appendChild(el("span", "", _t("分享链接")));
    const add = el("button", "link-btn h-act", _t("新建分享"));
    h.appendChild(add);
    wrap.appendChild(h);
    const list = el("div", "slist");
    wrap.appendChild(list);
    const refresh = () => {
      list.innerHTML = "";
      list.appendChild(el("div", "empty-row", _t("正在读取…")));
      api(`/api/cases/${encodeURIComponent(caseId)}/shares`).then((rows) => {
        list.innerHTML = "";
        if (!rows.length) list.appendChild(el("div", "empty-row", _t("还没有分享。可以把打样包发给工厂，或把视图和申请材料发给代理师，链接到期自动失效。")));
        for (const r of rows) {
          const row = el("div", "srow" + (r.status === "active" ? "" : " closed"));
          row.appendChild(el("span", "st", `${r.recipient} · ${PURPOSE_ZH[r.purpose] || r.purpose}${r.version ? " · " + r.version : ""}`));
          row.appendChild(el("span", "ss", _tf("{0} · {1} 次查看 · {2} 次下载 · {3}", SHARE_STATUS_ZH[r.status] || r.status, r.views, r.downloads, r.status === "active" ? _t("到期 ") + day(r.expires_at) : _t("关闭于 ") + day(r.closed_at))));
          const acts = el("span", "sa");
          if (r.status === "active" && r.url) {
            const c = el("button", "link-btn", _t("复制链接"));
            c.addEventListener("click", () => copyText(r.url));
            acts.appendChild(c);
          }
          const log = el("button", "link-btn", _t("访问记录"));
          log.addEventListener("click", async () => {
            const open = row.nextSibling && row.nextSibling.classList && row.nextSibling.classList.contains("slog");
            if (open) return row.nextSibling.remove();
            try {
              const d = await api(`/api/shares/${encodeURIComponent(r.share_id)}`);
              const box = el("div", "slog");
              const items = (d.access || []).slice(0, 20);
              if (!items.length) box.appendChild(el("div", "", _t("还没有人打开过这个链接。")));
              for (const a of items) {
                const what = a.kind === "view" ? _t("打开页面") : a.kind === "download" ? _tf("下载 {0}", a.file || "") : _t("链接已失效，被拒绝");
                box.appendChild(el("div", "", `${day(a.at)} · ${what} · ${[a.country, a.ip_prefix].filter(Boolean).join(" ") || _t("地址未知")}`));
              }
              row.after(box);
            } catch (err) {
              toast(errText(err), "error");
            }
          });
          acts.appendChild(log);
          if (r.status === "active") {
            const x = el("button", "link-btn", _t("撤回"));
            x.addEventListener("click", async () => {
              if (!window.confirm(_tf("撤回给「{0}」的分享？链接会立即失效，服务器上的文件会删除。", r.recipient))) return;
              try {
                await api(`/api/shares/${encodeURIComponent(r.share_id)}`, { method: "DELETE" });
                toast(_t("已撤回"));
                refresh();
              } catch (err) {
                toast(errText(err), "error");
              }
            });
            acts.appendChild(x);
          }
          row.appendChild(acts);
          list.appendChild(row);
        }
      }).catch(() => {
        list.innerHTML = "";
        list.appendChild(el("div", "empty-row", _t("读取分享链接失败（需要联网和 API Key）。")));
      });
    };
    add.addEventListener("click", () => openShare(caseId, refresh));
    refresh();
    return wrap;
  }

  let shareDone = null;
  function openShare(caseId, done) {
    const dlg = $("share-dlg");
    dlg.dataset.case = caseId;
    shareDone = done;
    $("share-result").classList.add("hidden");
    $("share-result").innerHTML = "";
    $("share-go").disabled = false;
    $("share-go").textContent = _t("生成链接");
    $("share-cancel").textContent = _t("取消");
    $("share-to").value = "";
    $("share-note").value = "";
    $("share-title").textContent = _tf("分享案卷 {0}", caseId);
    dlg.showModal();
    $("share-to").focus();
  }
  $("share-form").addEventListener("submit", async (e) => {
    if (!e.submitter || e.submitter.value !== "create") return; // cancel closes the dialog
    e.preventDefault();
    const to = $("share-to").value.trim();
    if (!to) return $("share-to").focus();
    const go = $("share-go");
    go.disabled = true;
    go.textContent = _t("正在加水印并上传…");
    try {
      const data = await api(`/api/cases/${encodeURIComponent($("share-dlg").dataset.case)}/shares`, { method: "POST", body: JSON.stringify({ to, purpose: $("share-purpose").value, days: Number($("share-days").value), note: $("share-note").value.trim(), watermark: $("share-wm").checked }) });
      const box = $("share-result");
      box.innerHTML = "";
      box.appendChild(el("div", "", _tf("已生成：{0} 个文件，{1} 前有效。", data.files.length, day(data.expires_at))));
      box.appendChild(el("span", "share-url", data.url));
      const copy = el("button", "link-btn", _t("复制链接"));
      copy.type = "button";
      copy.addEventListener("click", () => copyText(data.url));
      box.appendChild(copy);
      box.appendChild(el("div", "note", _t("只把链接发给接收方：持有链接的人都能打开。每次打开都有记录，可以随时撤回。")));
      box.classList.remove("hidden");
      go.textContent = _t("已生成");
      $("share-cancel").textContent = _t("完成");
      shareDone?.();
    } catch (err) {
      go.disabled = false;
      go.textContent = _t("生成链接");
      toast(errText(err), "error");
    }
  });

  // ------------------------------------------------- version compare (FR-EVAL-06)

  async function showCompare(caseId, a, b) {
    const body = $("panel-body");
    body.innerHTML = "";
    body.appendChild(el("div", "empty-row", _tf("正在对比 {0} 和 {1}…", a, b)));
    let data;
    try {
      data = await api(`/api/cases/${encodeURIComponent(caseId)}/compare?a=${a}&b=${b}`);
    } catch (e) {
      toast(e.message, "error");
      return showVersion(caseId, b);
    }
    body.innerHTML = "";
    const back = el("button", "link-btn back", _tf("← 返回 {0}", b));
    back.addEventListener("click", () => showVersion(caseId, b));
    body.appendChild(back);
    const head = el("div", "vdetail-h");
    head.appendChild(el("b", "", _tf("{0} → {1} 同比例叠图", a, b)));
    body.appendChild(head);
    const img = el("img", "vsheet");
    img.alt = _t("同比例叠图");
    img.src = fileUrl(data.sheet);
    img.addEventListener("click", () => lightbox(data.sheet));
    body.appendChild(img);
    body.appendChild(el("p", "legend", _tf("红色只在 {0}，蓝色只在 {1}，紫色是重合部分。重合度越低，外形改动越大。", a, b)));
    const facts = el("dl", "kv");
    const row = (k, v) => {
      facts.appendChild(el("dt", "", k));
      facts.appendChild(el("dd", "", v));
    };
    const s = data.size_mm || {};
    if (s.xlen) row(_t("外形尺寸"), `${s.xlen[0]}×${s.ylen[0]}×${s.zlen[0]} → ${s.xlen[1]}×${s.ylen[1]}×${s.zlen[1]} mm`);
    const ra = data.risk_a, rb = data.risk_b;
    row(_t("风险"), `${ra ? ra.max_risk.toFixed(2) + _t("（") + (RISK_ZH[ra.level] || ra.level) + _t("）") : _t("未评估")} → ${rb ? rb.max_risk.toFixed(2) + _t("（") + (RISK_ZH[rb.level] || rb.level) + _t("）") : _t("未评估")}`);
    const overlap = data.overlap || {};
    const low = Object.entries(overlap).sort((x, y) => x[1].iou - y[1].iou).slice(0, 3);
    if (low.length) row(_t("改动最大的视图"), low.map(([k, r]) => _tf("{0} 重合 {1}", k, r.iou.toFixed(2))).join(_t("，")));
    body.appendChild(facts);
    const changed = Object.entries(data.changed_params || {});
    if (changed.length) {
      body.appendChild(el("h4", "", _t("参数变化")));
      const table = el("table", "params");
      for (const [name, c] of changed.slice(0, 30)) {
        const tr = el("tr");
        tr.appendChild(el("td", "", name));
        tr.appendChild(el("td", "num", `${c.from ?? "—"} → ${c.to ?? "—"}`));
        table.appendChild(tr);
      }
      body.appendChild(table);
    }
  }

  // --------------------------------------------------- parameter sliders (FR-GEN-12)

  function paramControls(caseId, vid, params, schema) {
    const wrap = el("div", "tweak");
    const changes = {};
    const status = el("div", "tweak-status", _t("拖动滑杆改参数，然后预览。会生成一个新版本并做快速复检（不花积分）。"));
    for (const name of Object.keys(params).slice(0, 40)) {
      const spec = schema[name] || {};
      const row = el("div", "trow");
      row.appendChild(el("label", "", name));
      const value = params[name];
      let input;
      if (Array.isArray(spec.choices) && spec.choices.length) {
        input = el("select");
        for (const c of spec.choices) {
          const o = el("option", "", String(c));
          o.value = String(c);
          if (String(c) === String(value)) o.selected = true;
          input.appendChild(o);
        }
      } else if (typeof value === "number" && spec.min != null && spec.max != null) {
        input = el("input");
        input.type = "range";
        input.min = spec.min;
        input.max = spec.max;
        input.step = Number.isInteger(value) && Number.isInteger(spec.min) && Number.isInteger(spec.max) && (spec.max - spec.min) <= 50 ? 1 : (spec.max - spec.min) / 200;
        input.value = value;
      } else {
        row.appendChild(el("span", "tval", `${value}${spec.unit || ""}`));
        wrap.appendChild(row);
        continue;
      }
      const shown = el("span", "tval", `${value}${spec.unit || ""}`);
      input.addEventListener("input", () => {
        const v = input.type === "range" ? Number(input.value) : input.value;
        const rounded = typeof v === "number" ? Math.round(v * 10) / 10 : v;
        shown.textContent = `${rounded}${spec.unit || ""}`;
        if (String(rounded) === String(value)) delete changes[name];
        else changes[name] = rounded;
        row.classList.toggle("changed", name in changes);
        go.disabled = !Object.keys(changes).length;
      });
      row.appendChild(input);
      row.appendChild(shown);
      wrap.appendChild(row);
    }
    const go = el("button", "btn", _t("预览这个改动"));
    go.disabled = true;
    go.addEventListener("click", async () => {
      go.disabled = true;
      status.textContent = _t("正在重建模型并快速复检…");
      try {
        const r = await api(`/api/cases/${encodeURIComponent(caseId)}/versions/${vid}/tweak`, { method: "POST", body: JSON.stringify({ set: changes, rationale: _t("工作台参数微调") }) });
        const s = r.summary;
        const pending = s && (s.pending_observer || []).length;
        toast(_tf("已生成 {0}（{1} 秒）", r.version, (r.total_ms / 1000).toFixed(1)) + (!s ? "" : pending ? _tf("；最像的 {0} 件还要观察者评分才有风险结论", pending) : _tf(" · 风险 {0}", s.max_risk.toFixed(2))), "ok");
        showVersion(caseId, r.version);
      } catch (e) {
        status.textContent = _t("这个改动建不出来：") + e.message.slice(0, 160);
        go.disabled = false;
      }
    });
    wrap.appendChild(status);
    wrap.appendChild(go);
    return wrap;
  }

  async function showVersion(caseId, vid) {
    const body = $("panel-body");
    body.innerHTML = "";
    body.appendChild(el("div", "empty-row", _t("正在打开 ") + vid + "…"));
    let data;
    try {
      data = await api(`/api/cases/${encodeURIComponent(caseId)}/versions/${vid}`);
    } catch (e) {
      toast(e.message, "error");
      return showCase(caseId);
    }
    body.innerHTML = "";
    const back = el("button", "link-btn back", _t("← 返回案卷"));
    back.addEventListener("click", () => showCase(caseId));
    body.appendChild(back);
    const v = data.version;
    const head = el("div", "vdetail-h");
    head.appendChild(el("span", "num", v.id));
    head.appendChild(el("b", "", v.label && v.label !== v.id ? v.label : v.kind === "original" ? _t("原始设计") : v.kind === "draft" ? _t("3D 草稿") : _t("候选方案")));
    body.appendChild(head);
    if (data.sheet) {
      const img = el("img", "vsheet");
      img.alt = _t("七视图渲染");
      img.src = fileUrl(data.sheet);
      img.addEventListener("click", () => lightbox(data.sheet));
      body.appendChild(img);
    }
    const facts = el("dl", "kv");
    const row = (k, val) => {
      if (val === undefined || val === null || val === "") return;
      facts.appendChild(el("dt", "", k));
      facts.appendChild(el("dd", "", String(val)));
    };
    row(_t("风险"), v.risk_provisional ? _t("待观察者评分（目前只有轮廓比较，不能当作风险）") : typeof v.risk === "number" ? _tf("{0}（{1}）", v.risk.toFixed(2), RISK_ZH[v.risk_level] || v.risk_level || "") : _t("未评估"));
    row(_t("合格"), v.qualified ? _t("是") : v.qualified === false ? _t("否") : "");
    row(_t("来源方向"), v.concept);
    row(_t("3D 审核"), v.review && v.review.match != null ? _tf("与概念图一致度 {0}/10{1}", v.review.match, v.kind === "draft" ? _t("（未通过）") : "") : "");
    row(_t("上一版"), v.parent);
    row(_t("说明"), v.rationale);
    body.appendChild(facts);
    const left = ((v.review && v.review.mismatches) || []).map((m) => (typeof m === "string" ? { issue: m } : m)).filter((m) => m && m.issue);
    if (left.length) {
      body.appendChild(el("h4", "", v.kind === "draft" ? _t("审核员还看到的差异") : _t("审核员记录的小差异")));
      const ul = el("ul", "mismatch");
      for (const m of left.slice(0, 8)) ul.appendChild(el("li", m.severity === "detail" ? "detail" : "major", _tf("{0}：{1}", m.severity === "detail" ? _t("细节") : _t("主要"), m.issue)));
      body.appendChild(ul);
    }
    const params = (data.params && data.params.params) || {};
    const schema = (data.params && data.params.schema) || {};
    const names = Object.keys(params);
    if (names.length) {
      body.appendChild(el("h4", "", _t("参数（可直接调整）")));
      body.appendChild(paramControls(caseId, vid, params, schema));
    }
    const actions = el("div", "panel-actions");
    if (v.risk_provisional) {
      const obs = el("button", "btn primary", _t("让观察者复检这个版本"));
      obs.addEventListener("click", () => sendText(_tf("案卷 {0}：让观察者给 {1} 和最像的几件在先设计复检，然后告诉我风险。", caseId, vid)));
      actions.appendChild(obs);
    }
    if (data.original && data.original !== vid) {
      const cmp = el("button", "btn ghost", _tf("与原始设计 {0} 对比", data.original));
      cmp.addEventListener("click", () => showCompare(caseId, data.original, vid));
      actions.appendChild(cmp);
    }
    const change = el("button", "btn", _t("在对话里修改这个版本"));
    change.addEventListener("click", () => {
      $("panel").classList.add("hidden");
      $("input").value = _tf("修改案卷 {0} 的 {1}：", caseId, vid);
      $("input").focus();
    });
    actions.appendChild(change);
    body.appendChild(actions);
  }

  function sendText(text) {
    $("panel").classList.add("hidden");
    state.caseId = null;
    $("input").value = text;
    send();
  }

  $("panel-close").addEventListener("click", () => {
    $("panel").classList.add("hidden");
    state.caseId = null;
    loadCases();
  });

  async function openSession(id) {
    if (state.source) state.source.close();
    state.session = id;
    state.current = null;
    const data = await api("/api/sessions/" + id);
    const firstUser = (data.items || []).find((i) => i.role === "user");
    $("chat-title").textContent = data.name || (firstUser ? firstUser.text.split("\n")[0].slice(0, 60) : _t("对话"));
    renderTranscript(data.items);
    setBusy(data.busy);
    connect(id);
    loadSessions();
  }

  async function newSession() {
    const { id } = await api("/api/sessions", { method: "POST", body: "{}" });
    await openSession(id);
    $("messages").innerHTML = "";
    $("messages").appendChild(welcomeNode);
    bindExamples();
    $("chat-title").textContent = _t("新对话");
  }

  function connect(id) {
    const source = new EventSource("/api/sessions/" + id + "/events");
    state.source = source;
    source.onmessage = (msg) => {
      const ev = JSON.parse(msg.data);
      if (ev.session && ev.session !== state.session) return;
      switch (ev.type) {
        case "hello":
          setBusy(ev.busy);
          break;
        case "assistant_start":
          setBusy(true);
          state.current = assistantBlock();
          setAssistantText(state.current, "");
          break;
        case "thinking":
          if (state.current && !state.current.text) state.current.body.classList.add("thinking");
          break;
        case "text_delta":
          if (!state.current) state.current = assistantBlock();
          state.current.body.classList.remove("thinking");
          setAssistantText(state.current, state.current.text + ev.delta);
          scrollDown();
          break;
        case "assistant_end":
          if (typeof ev.chat_spent === "number") setMeter(ev.chat_spent);
          if (state.current) {
            if (ev.text) setAssistantText(state.current, ev.text);
            if (!state.current.text) state.current.row.remove();
            else if (ev.wording) state.current.body.appendChild(el("div", "wording-fix", ev.wording.note));
          }
          if (ev.error) showError(ev.error);
          state.current = null;
          break;
        case "tool_start":
          toolCard(ev.id, ev.name, ev.args);
          break;
        case "tool_progress": {
          const t = state.tools[ev.id];
          if (t) {
            t.log.textContent += (t.log.textContent ? "\n" : "") + ev.line;
            t.log.scrollTop = t.log.scrollHeight;
          }
          break;
        }
        case "tool_end":
          finishTool(ev);
          if (["case_create", "model_from_template", "model_import_cad", "cad_tweak", "cad_program", "screen", "design_around", "design_build", "design_concepts", "design_accept", "batch_screen"].includes(ev.name)) loadCases();
          // FR-UX-06: long steps tell the person when they finish while the window is in the background.
          if (LONG_TOOLS[ev.name] && (document.hidden || !document.hasFocus()) && (ev.ms || 0) > 20000) {
            api("/api/notify", { method: "POST", body: JSON.stringify({ title: _tf("DesignAround：{0}{1}", LONG_TOOLS[ev.name], ev.ok === false ? _t("没有完成") : _t("完成")), text: ev.ok === false ? _t("打开工作台看看原因。") : _t("回到工作台查看结果。") }) }).catch(() => {});
          }
          break;
        case "confirm":
          askConfirm(ev);
          break;
        case "confirm_closed":
          if ($("confirm").dataset.id === ev.id && $("confirm").open) $("confirm").close("closed");
          break;
        case "retry":
          $("messages").appendChild(el("div", "note", _tf("模型请求重试 {0}/{1}", ev.attempt, ev.max)));
          break;
        case "settled":
          setBusy(false);
          loadSessions();
          break;
        case "error":
          setBusy(false);
          showError(ev.message);
          break;
      }
    };
  }

  // ------------------------------------------------------------- confirm

  function askConfirm(ev) {
    const dlg = $("confirm");
    dlg.dataset.id = ev.id;
    const en = window.DA_LANG === "en";
    $("confirm-title").textContent = en && ev.title_en ? ev.title_en : ev.title;
    $("confirm-detail").textContent = en && ev.detail_en ? ev.detail_en : ev.detail;
    $("confirm-credits").textContent = ev.credits ? fmt(ev.credits) : _t("免费");
    $("confirm-usd").textContent = ev.credits ? _tf("积分以内，约 US${0}", (ev.credits / 100).toFixed(2)) : _t("不扣积分，但需要你确认");
    dlg.returnValue = "";
    dlg.showModal();
  }
  $("confirm").addEventListener("close", async () => {
    const dlg = $("confirm");
    if (dlg.returnValue === "closed" || !dlg.dataset.id) return;
    const approve = dlg.returnValue === "yes";
    await api("/api/confirm", { method: "POST", body: JSON.stringify({ id: dlg.dataset.id, approve }) }).catch(() => {});
    dlg.dataset.id = "";
  });

  // ------------------------------------------------------------- composer

  async function send() {
    const text = $("input").value.trim();
    if (!text && !state.attachments.length) return;
    if (!state.session) await newSession();
    const files = state.attachments.map((a) => a.path);
    if ($("chat-title").textContent === _t("新对话") && text) $("chat-title").textContent = text.slice(0, 60);
    addUser(text, files);
    $("input").value = "";
    $("input").style.height = "auto";
    state.attachments = [];
    renderAttachments();
    setBusy(true);
    try {
      await api(`/api/sessions/${state.session}/messages`, { method: "POST", body: JSON.stringify({ text, attachments: files }) });
    } catch (e) {
      setBusy(false);
      showError(e.message);
    }
  }

  function renderAttachments() {
    const box = $("attachments");
    box.innerHTML = "";
    for (const a of state.attachments) {
      const chip = el("span", "file-chip", a.name);
      const x = el("button", "chip-x", "×");
      x.addEventListener("click", () => {
        state.attachments = state.attachments.filter((b) => b !== a);
        renderAttachments();
      });
      chip.appendChild(x);
      box.appendChild(chip);
    }
  }

  $("file").addEventListener("change", async (e) => {
    for (const file of e.target.files) {
      const res = await fetch("/api/upload?name=" + encodeURIComponent(file.name), { method: "POST", body: file, credentials: "same-origin" });
      if (!res.ok) {
        toast(_tf("{0}：{1}", file.name, await res.text()), "error");
        continue;
      }
      state.attachments.push(await res.json());
    }
    e.target.value = "";
    renderAttachments();
  });

  $("send").addEventListener("click", send);
  $("input").addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey && !e.isComposing) {
      e.preventDefault();
      send();
    }
  });
  $("input").addEventListener("input", (e) => {
    e.target.style.height = "auto";
    e.target.style.height = Math.min(200, e.target.scrollHeight) + "px";
  });
  $("abort").addEventListener("click", () => state.session && api(`/api/sessions/${state.session}/abort`, { method: "POST", body: "{}" }));
  $("new-chat").addEventListener("click", newSession);
  if (!/Mac|iPhone|iPad/.test(navigator.platform || navigator.userAgent)) {
    const kbd = document.querySelector("#new-chat .kbd");
    if (kbd) kbd.textContent = "Ctrl+N";
  }
  document.addEventListener("keydown", (e) => {
    if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "n") {
      e.preventDefault();
      newSession();
    }
  });

  const welcomeNode = $("welcome");
  function bindExamples() {
    for (const b of document.querySelectorAll(".example")) b.onclick = () => {
      $("input").value = b.querySelector(".ex-t")?.textContent || b.textContent;
      $("input").focus();
    };
    // "What do you have?" adds the entry path (FR-3D-12) to whatever the person typed.
    for (const c of document.querySelectorAll(".chip[data-have]")) c.onclick = () => {
      const have = c.dataset.have;
      const now = $("input").value.trim();
      $("input").value = now ? (now.includes(have) ? now : `${now}\n${have}`) : have;
      $("input").focus();
    };
    const sample = $("try-sample");
    if (sample) sample.onclick = async () => {
      sample.disabled = true;
      sample.textContent = _t("正在生成示例（约半分钟）…");
      try {
        const r = await api("/api/sample", { method: "POST", body: "{}" });
        toast(_t("示例案卷已生成：数据是演示用的，不花积分"), "ok");
        await loadCases();
        showCase(r.case);
      } catch (e) {
        toast(e.message, "error");
      } finally {
        sample.disabled = false;
        sample.textContent = _t("先看示例案卷（免费）");
      }
    };
  }
  bindExamples();

  // ------------------------------------------------------------- settings

  $("settings-btn").addEventListener("click", async () => {
    const s = await api("/api/state");
    $("set-budget").value = s.settings.chatBudget;
    $("set-auto").value = s.settings.autoApproveCredits;
    $("set-info").textContent = _tf("模式 {0} · 助手模型 {1} · Pi 配置目录 {2}{3}", s.mode, s.models.assistant, s.pi_agent_dir, s.isolated ? _t("（与本机 Pi 隔离）") : "");
    const n = await api("/api/notify-settings").catch(() => ({ webhook: "", desktop: false }));
    $("set-webhook").value = n.webhook || "";
    $("set-desktop").checked = !!n.desktop;
    $("set-lang").value = window.DA_LANG;
    $("settings").showModal();
  });
  $("settings").addEventListener("close", async () => {
    if ($("settings").returnValue !== "save") return;
    const saved = await api("/api/settings", { method: "PUT", body: JSON.stringify({ chatBudget: Number($("set-budget").value), autoApproveCredits: Number($("set-auto").value) }) });
    state.budget = saved.chatBudget;
    setMeter(state.spent || 0);
    try {
      await api("/api/notify-settings", { method: "PUT", body: JSON.stringify({ webhook: $("set-webhook").value.trim(), desktop: $("set-desktop").checked }) });
    } catch (e) {
      toast(errText(e), "error");
    }
    if ($("set-lang").value !== window.DA_LANG) window.daSetLang($("set-lang").value); // reloads the page in the new language
  });

  const PLAN_ZH = { free: _t("免费版"), pro: "Pro", team: "Team", enterprise: "Enterprise" };
  function billingNote(text) {
    const n = $("billing-note");
    n.textContent = text;
    n.classList.toggle("hidden", !text);
  }
  $("manage-billing").addEventListener("click", async (e) => {
    e.preventDefault();
    billingNote("");
    $("billing-now").textContent = _t("正在读取账户…");
    $("billing").showModal();
    try {
      const b = await api("/api/billing");
      const parts = [_tf("当前：{0}{1}", PLAN_ZH[b.plan] || b.plan, b.is_trial ? _t("（试用）") : "")];
      if (b.balance != null) parts.push(_tf("余额 {0} 积分（约 US${1}）", fmt(b.balance), (b.balance / 100).toFixed(2)));
      if (b.subscription_status) parts.push(_tf("订阅状态 {0}", b.subscription_status));
      $("billing-now").textContent = parts.join(" · ");
      $("billing-portal").classList.toggle("hidden", !b.can_manage_billing);
    } catch (err) {
      $("billing-now").textContent = _t("读取账户失败：") + err.message;
    }
  });
  for (const b of document.querySelectorAll("[data-checkout]")) {
    b.addEventListener("click", async () => {
      const kind = b.dataset.checkout;
      const body = kind === "pro" ? { plan: "pro" } : kind === "pro-annual" ? { plan: "pro", annual: true } : { credits: "5k" };
      b.disabled = true;
      billingNote(_t("正在创建付款链接…"));
      try {
        const r = await api("/api/billing/checkout", { method: "POST", body: JSON.stringify(body) });
        if (r.url) {
          window.open(r.url, "_blank", "noopener");
          billingNote(_t("已在浏览器打开付款页面。付款完成后回到这里，额度会自动更新。"));
        } else billingNote(r.message || _t("暂时无法创建付款链接。"));
      } catch (err) {
        billingNote(err.message);
      } finally {
        b.disabled = false;
      }
    });
  }
  $("billing-portal").addEventListener("click", async () => {
    try {
      const r = await api("/api/billing/portal", { method: "POST", body: "{}" });
      if (r.url) window.open(r.url, "_blank", "noopener");
      else billingNote(r.message || _t("这个账户还没有付费订阅。"));
    } catch (err) {
      billingNote(err.message);
    }
  });

  // ------------------------------------------------------------- start

  (async function start() {
    const s = await api("/api/state");
    $("runtime").textContent = `${s.mode} · ${s.models.assistant}`;
    state.budget = s.settings.chatBudget;
    setMeter(0);
    await Promise.all([loadSessions(), loadCases()]);
    const { id } = await api("/api/sessions", { method: "POST", body: "{}" });
    state.session = id;
    connect(id);
    for (const c of s.pending_confirms || []) if (c.session === id) askConfirm(c);
  })().catch((e) => toast(e.message, "error"));
})();
