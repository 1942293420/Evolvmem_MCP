/* Shared create/edit form for user-authored experiences. */
(function (root) {
  const E = root.EvolvMemV2;
  let editor = null;
  const ids = ["new-exp-cat", "new-exp-prob", "new-exp-sol", "new-exp-reus"];
  const byId = id => root.document && root.document.getElementById(id);
  const ownLibrary = () => E.state.libraryCtx === E.state.ownerId && !!E.state.ownerId && E.canWrite();
  const requestId = () => "manual-web-" + (root.crypto && root.crypto.randomUUID
    ? root.crypto.randomUUID() : Date.now().toString(36) + "-" + Math.random().toString(36).slice(2));
  E.canEditManualExperience = note => !!note && ownLibrary() && note.origin === "manual" &&
    note.status === "active" && Number.isInteger(note.revision) && note.revision > 0;

  function current(form) {
    return editor === form && form.state === E.state && form.library === E.state.libraryCtx &&
      (!form.ticket || (E.session === form.session && E.session.isCurrent(form.ticket)));
  }
  function status(message, error) {
    const node = byId("new-exp-status");
    if (node) { node.textContent = message; node.setAttribute("role", error ? "alert" : "status"); }
  }
  function busy(form, saving) {
    ids.forEach(id => { const el = byId(id); if (el) el.disabled = saving || form.saved || form.unknown; });
    const reload = byId("new-exp-reload"); if (reload) { reload.hidden = !form.conflict && !form.refreshFailed; reload.disabled = saving; reload.textContent = form.saved ? "重新刷新列表" : "重新读取记录"; }
    const button = byId("new-exp-submit");
    if (button) {
      button.disabled = saving || form.saved || form.conflict;
      button.textContent = saving ? "正在保存…" : form.saved ? "已保存" : form.unknown ? "核对并重试" : form.note ? "保存修改" : "保存经验";
      button.setAttribute("aria-busy", String(saving));
    }
  }
  function feedback(code) {
    if (code === "experience_revision_conflict" || code === "experience_state_conflict")
      return "这条经验已发生变化。你的输入已保留，请关闭窗口并重新读取记录，核对后重新编辑；本次没有覆盖。";
    if (code === "experience_request_id_conflict")
      return "保存请求与先前内容不一致。你的输入已保留，请关闭窗口并重新读取记录后再保存。";
    if (/embedding/.test(code))
      return "向量编码未完成，保存失败；你的输入已保留，可以稍后重试。（" + code + "）";
    if (/revision/.test(code)) return "记录版本无效。你的输入已保留，请重新读取记录后再编辑。";
    return "保存失败，输入已保留。（" + code + "）";
  }

  E.openExperienceEditor = function (note, restoring) {
    if (!restoring && editor && current(editor) && (editor.unknown || (editor.saving && !editor.saved))) {
      const pending=E.captureExperienceEditorDraft();
      if (pending) return E.restoreExperienceEditorDraft(pending);
    }
    if (!ownLibrary() || (note && !E.canEditManualExperience(note))) return false;
    const form = { note: note ? Object.assign({}, note) : null, state: E.state, library: E.state.libraryCtx,
      session: E.session || null, ticket: E.session ? E.session.capture() : null,
      saving: false, saved: false, conflict: false, fingerprint: "", requestId: "" };
    editor = form;
    const esc = E.esc, value = note || {};
    E.openModal(note ? "编辑手工经验" : "新增手工经验", `
      <p class="secondary">每条只记录一个问题。保存后会同时更新正文和检索向量。</p>
      <label><div><strong>经验分类</strong></div><select id="new-exp-cat" style="width:100%">
        ${["系统环境经验","业务经验","前端经验"].map(cat => '<option' + (value.category === cat ? ' selected' : '') + '>' + esc(cat) + '</option>').join("")}
      </select></label>
      <label><div><strong>核心问题</strong></div><textarea id="new-exp-prob" rows="3" maxlength="1000" style="width:100%" aria-describedby="new-exp-status new-exp-prob-error">${esc(value.problem || "")}</textarea><span id="new-exp-prob-error" style="display:block;color:var(--danger)" aria-live="polite"></span></label>
      <label><div><strong>解决办法</strong></div><textarea id="new-exp-sol" rows="4" maxlength="8000" style="width:100%" aria-describedby="new-exp-status new-exp-sol-error">${esc(value.solution || "")}</textarea><span id="new-exp-sol-error" style="display:block;color:var(--danger)" aria-live="polite"></span></label>
      <label><div><strong>可复用经验</strong></div><textarea id="new-exp-reus" rows="3" maxlength="8000" style="width:100%" aria-describedby="new-exp-status new-exp-reus-error">${esc(value.reusable || value.reusable_experience || "")}</textarea><span id="new-exp-reus-error" style="display:block;color:var(--danger)" aria-live="polite"></span></label>
      <p id="new-exp-status" role="status" aria-live="polite" style="min-height:2.8em">填写完整后保存。</p>
      <div style="display:flex;gap:8px;justify-content:flex-end;flex-wrap:wrap">
        <button type="button" id="new-exp-reload" class="btn" data-act="refresh-manual-record" hidden>重新读取记录</button>
        <button type="button" class="btn" data-act="close-modal">关闭</button>
        <button type="button" id="new-exp-submit" class="btn btn-primary" data-act="submit-new-exp">${note ? "保存修改" : "保存经验"}</button>
      </div>`);
    return true;
  };


  const clone = value => value === undefined ? undefined : JSON.parse(JSON.stringify(value));
  const unknownMessage = "保存结果暂未确认，输入已保留。请点击“核对并重试”核对同一请求；确认前暂不修改内容。";
  E.captureExperienceEditorDraft = function () {
    const form=editor;
    if (!form || !current(form) || (!byId("new-exp-status") && !form.unknown && !form.saving)) return null;
    return clone({version:1,scope:E.session?.scope || "",owner:E.state.ownerId,library:form.library,
      note:form.note,fingerprint:form.fingerprint,requestId:form.requestId,pendingPayload:form.pendingPayload,
      unknown:form.unknown || (form.saving && !form.saved),saved:form.saved,savedNote:form.savedNote,
      conflict:form.conflict,refreshFailed:form.refreshFailed || (form.saving && form.saved)});
  };
  E.discardExperienceEditor = () => {editor=null;};
  E.restoreExperienceEditorDraft = function (draft) {
    if (!draft || draft.version!==1 || draft.scope!==(E.session?.scope || "") ||
        draft.owner!==E.state.ownerId || draft.library!==E.state.libraryCtx || !ownLibrary()) return false;
    if (!E.openExperienceEditor(draft.note || undefined,true)) return false;
    const form=editor;
    for (const key of ["fingerprint","requestId","pendingPayload","unknown","saved","savedNote","conflict","refreshFailed"])
      if (draft[key]!==undefined) form[key]=clone(draft[key]);
    if (form.unknown) {
      const pending=form.pendingPayload;
      if (!pending || pending.request_id!==form.requestId ||
          pending.action!==(form.note ? "update" : "create") ||
          (form.note && (pending.id!==form.note.id || pending.expected_revision!==form.note.revision))) {
        editor=null; E.closeModal(); return false;
      }
      for (const [id,key] of [[ids[0],"category"],[ids[1],"problem"],[ids[2],"solution"],[ids[3],"reusable_experience"]]) {
        const input=byId(id); if (input) input.value=pending[key];
      }
    }
    busy(form,false);
    if (form.unknown) status(unknownMessage,true);
    else if (form.saved) status(savedStatus(form,!form.refreshFailed),!!form.refreshFailed);
    else if (form.conflict) status(feedback("experience_revision_conflict"),true);
    return true;
  };
  function savedStatus(form, refreshed) {
    const saved=form.savedNote || {};
    const inList=E.state.experiences.some(note=>note.id===(saved.id || form.note?.id));
    return "保存成功" + (saved.revision ? "，当前版本 "+saved.revision : "") +
      (!refreshed ? "；列表或概览刷新失败，当前搜索与位置已保留，可重新刷新。" :
       inList ? "。列表和详情已刷新，搜索与页码已保留。" : "。该记录不在当前筛选结果中；搜索与页码已保留。");
  }
  E.refreshExperienceEditor = async function () {
    const form=editor;
    if (!form || !current(form) || form.saving || (!form.conflict && !form.refreshFailed) || !ownLibrary()) return false;
    const button=byId("new-exp-reload"); if (button) button.disabled=true;
    try {
      const refreshed=form.saved ? await E.api.refreshExperienceViews() :
        await E.api.loadExperiences({preserveOnError:true});
      if (!current(form)) return false;
      E.renderAll();
      if (form.saved) { form.refreshFailed=!refreshed; status(savedStatus(form,refreshed),!refreshed); }
      else status(!refreshed ? "重新读取失败，输入已保留，请稍后重试。" :
        "已重新读取列表。你的输入仍保留；请关闭窗口，打开记录核对新版本后再编辑。", true);
      return !!refreshed;
    } finally { if (current(form)) busy(form,false); }
  };

  E.submitExperienceEditor = async function () {
    const form = editor;
    if (!form || !current(form) || form.saving || form.saved || form.conflict) return false;
    if (!ownLibrary() || (form.note && !E.canEditManualExperience(form.note))) return false;
    const payload = form.unknown ? clone(form.pendingPayload) : { action: form.note ? "update" : "create", category: (byId(ids[0])?.value || "").trim(),
      problem: (byId(ids[1])?.value || "").trim(), solution: (byId(ids[2])?.value || "").trim(),
      reusable_experience: (byId(ids[3])?.value || "").trim() };
    ids.slice(1).forEach(id => { const error = byId(id+"-error"); if (error) error.textContent = ""; });
    for (const [field, label, id] of [["problem","核心问题",ids[1]],["solution","解决办法",ids[2]],
                                     ["reusable_experience","可复用经验",ids[3]]]) {
      if (!payload[field]) {
        const error = byId(id+"-error"); if (error) error.textContent = "请填写" + label + "。";
        status("请填写" + label + "，不会用占位文字代替。", true);
        const input = byId(id); if (input) { input.setAttribute("aria-invalid","true"); input.focus(); }
        return false;
      }
      byId(id)?.setAttribute("aria-invalid","false");
    }
    if (form.note) { payload.id = form.note.id; payload.expected_revision = form.note.revision; }
    const fingerprint = form.unknown ? form.fingerprint : JSON.stringify(payload);
    if (form.fingerprint !== fingerprint) {
      form.fingerprint = fingerprint; form.requestId = requestId();
    }
    payload.request_id = form.requestId;
    form.pendingPayload = clone(payload);
    const list = root.document?.querySelector?.(".list-scroll");
    const scroll = { list: list?.scrollTop || 0, page: root.scrollY || 0 };
    form.saving = true; busy(form, true); status("正在保存并更新检索向量，请稍候。");
    if(E.session && E.session.save)E.session.save();
    try {
      const result = await E.api.writeExperience(payload, { returnError: true, deferRefresh: true });
      if (!current(form)) return false;
      const receipt=result && (result.note || result.experience);
      if (result && result.ok && (!receipt || typeof receipt.id!=="string" || !Number.isInteger(receipt.revision) || receipt.revision<1)) {
        form.unknown=true; status(unknownMessage,true); return false;
      }
      if (!result || !result.ok) {
        const code = String(result?.error || "request_failed");
        form.unknown = ["network_unavailable","request_failed","invalid_json","invalid_write_receipt"].includes(code) ||
          (!!result && result.status>=200 && result.status<300) ||
          (!!result && result.status>=500 && !/^(experience_embedding_|embedding_)/.test(code));
        form.conflict = !form.unknown && ["experience_revision_conflict","experience_state_conflict","experience_request_id_conflict"].includes(code);
        status(form.unknown ? unknownMessage : feedback(code), true);
        return false;
      }
      form.unknown = false;
      form.saved = true;
      const saved = result.note || result.experience || {};
      form.savedNote = saved;
      busy(form,false); status("保存成功，正在刷新列表和详情…");
      const refreshed = await E.api.refreshExperienceViews();
      if (!current(form)) return true;
      form.refreshFailed = !refreshed;
      const savedId = saved.id || form.note?.id;
      const inList = E.state.experiences.some(note => note.id === savedId);
      if (inList) E.state.selectedExpId = savedId;
      E.renderAll();
      const nextList = root.document?.querySelector?.(".list-scroll");
      if (nextList) nextList.scrollTop = scroll.list;
      if (root.scrollTo) root.scrollTo({top:scroll.page,behavior:"instant"});
      status(savedStatus(form,refreshed),!refreshed);
      return true;
    } catch (_err) {
      if (current(form)) {
        if (form.saved) { form.refreshFailed=true; status(savedStatus(form,false),true); return true; }
        form.unknown=true; status(unknownMessage,true);
      }
      return false;
    } finally {
      form.saving = false;
      if (current(form)) {busy(form, false);if(E.session && E.session.save)E.session.save();}
    }
  };
})(typeof window !== "undefined" ? window : globalThis);
