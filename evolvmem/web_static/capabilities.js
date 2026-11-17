(() => {
  'use strict';
  // One controller belongs to one retained workspace node. No persistent cache
  // is shared between users, and browsing never implicitly starts a write.
  const controllers = new WeakMap();
  let selectSequence = 0;
  window.EvolvCapabilities = {render({mount, esc, toast, initialTab='formal', onTab}) {
    if (controllers.has(mount)) {
      const existing = controllers.get(mount);
      existing.resume(initialTab);
      return existing;
    }
    const state = {
      query:'', draft:'', category:'all', source:'all', sourceScope:'current', status:'active', page:1, assignedPending:false,
      tab:['formal','intake','suggestions'].includes(initialTab)?initialTab:'formal',
      intakeQuery:'', intakeDraft:'', intakeCategory:'', intakeSource:'', intakePage:1,
      data:null, dataKey:'', expanded:new Set(), intakeChoices:new Map(),
      notice:'', error:false, loading:false, sequence:0, read:null,
      suspended:false, needsLoad:true, writing:false,
    };
    const cache = new Map();
    const MAX_CACHE_ENTRIES = 12, MAX_CACHE_BYTES = 4 * 1024 * 1024;
    let cacheBytes = 0;
    const renderedPanels = new Map();
    const $ = selector => mount.querySelector(selector);
    const selectWidgets = new Map();
    let dropdown = null;
    function closeSelect() {
      if (!dropdown) return;
      const {button, menu, release} = dropdown;
      dropdown = null;
      button.setAttribute('aria-expanded','false');
      button.removeAttribute('aria-activedescendant');
      menu.remove(); release();
    }
    function enhanceSelects() {
      for (const [select, widget] of selectWidgets) if (!mount.contains(select)) {
        if (dropdown?.select === select) closeSelect();
        widget.observer.disconnect(); selectWidgets.delete(select);
      }
      mount.querySelectorAll('select[data-memory-source],select[data-intake-category],select[data-intake-filter],select[data-intake-source]').forEach(select => {
        let widget = selectWidgets.get(select);
        if (!widget) {
          const wrapper = document.createElement('div'); wrapper.className='capability-select';
          if (select.hasAttribute('data-memory-source') || select.hasAttribute('data-intake-source') || select.hasAttribute('data-intake-filter')) wrapper.classList.add('capability-source-select');
          select.before(wrapper); wrapper.append(select);
          select.classList.add('capability-native-select'); select.tabIndex=-1; select.setAttribute('aria-hidden','true');
          const button = document.createElement('button'); button.type='button'; button.className='capability-select-trigger';
          button.setAttribute('role','combobox'); button.setAttribute('aria-haspopup','listbox'); button.setAttribute('aria-expanded','false');
          const menuId='capability-options-'+(++selectSequence); button.setAttribute('aria-controls',menuId);
          const caption=document.createElement('span'); caption.className='capability-select-value'; button.append(caption);
          const chevron=document.createElement('span'); chevron.className='capability-select-chevron'; chevron.setAttribute('aria-hidden','true'); chevron.innerHTML='<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="m6 9 6 6 6-6"/></svg>'; button.append(chevron);
          wrapper.append(button);
          const sync = () => {
            caption.textContent=select.selectedOptions[0]?.textContent || '请选择分类';
            button.setAttribute('aria-label',(select.getAttribute('aria-label') || '选择')+'：'+caption.textContent);
            button.disabled=select.disabled || !select.options.length;
            if (dropdown?.select===select && button.disabled) closeSelect();
          };
          const observer=new MutationObserver(sync); observer.observe(select,{attributes:true,attributeFilter:['disabled','selected'],childList:true,subtree:true});
          widget={button,menuId,observer,sync}; selectWidgets.set(select,widget);
          select.addEventListener('change',sync);
          button.addEventListener('click',()=>{if (dropdown?.select===select) closeSelect(); else openSelect(select);});
          button.addEventListener('keydown',event=>{
            if (button.disabled) return;
            const key=event.key, navigation=['ArrowDown','ArrowUp','Home','End'];
            if (navigation.includes(key)) {
              event.preventDefault();
              if (!dropdown || dropdown.select!==select) {openSelect(select); if(key==='Home'||key==='End') dropdown?.move(key);}
              else dropdown.move(key);
            } else if (key==='Escape') {if(dropdown){event.preventDefault();closeSelect();}}
            else if (key==='Tab') closeSelect();
            else if (key==='Enter'||key===' ') {
              event.preventDefault();
              if (dropdown?.select===select) dropdown.choose(); else openSelect(select);
            } else if (key.length===1 && !event.ctrlKey && !event.altKey && !event.metaKey) {
              event.preventDefault(); if(dropdown?.select!==select)openSelect(select); dropdown?.search(key);
            }
          });
        }
        widget.sync();
      });
    }
    function openSelect(select) {
      closeSelect();
      const widget=selectWidgets.get(select); if(!widget || widget.button.disabled)return;
      const {button,menuId}=widget;
      const options=[...select.options]; const enabled=options.map((o,i)=>!o.disabled?i:-1).filter(i=>i>=0); if(!enabled.length)return;
      const menu=document.createElement('div'); menu.className='capability-select-menu'; menu.id=menuId; menu.setAttribute('role','listbox');
      menu.setAttribute('aria-label',select.getAttribute('aria-label')||'选择分类');
      let active=enabled.includes(select.selectedIndex)?select.selectedIndex:enabled[0], searchText='',searchTime=0;
      options.forEach((option,index)=>{
        const row=document.createElement('div'); row.className='capability-select-option'; row.id=menuId+'-'+index; row.dataset.optionIndex=index;
        row.setAttribute('role','option'); row.setAttribute('aria-selected',String(option.selected)); row.setAttribute('aria-disabled',String(option.disabled));
        const text=document.createElement('span');text.textContent=option.textContent;row.append(text);
        const mark=document.createElement('span');mark.className='capability-select-check';mark.setAttribute('aria-hidden','true');mark.textContent=option.selected?'✓':'';row.append(mark);menu.append(row);
      });
      document.body.append(menu);
      const bounds=button.getBoundingClientRect(),width=Math.min(Math.max(bounds.width,220),innerWidth-24);
      menu.style.width=width+'px';menu.style.left=Math.max(12,Math.min(bounds.left,innerWidth-width-12))+'px';
      const header=document.querySelector('.refresh-topnav');
      const topLimit=Math.max(12,(header?.getBoundingClientRect().bottom || 0)+8),bottomLimit=innerHeight-12,gap=6;
      const desired=Math.min(menu.scrollHeight+2,300);
      const below=Math.max(0,bottomLimit-Math.max(topLimit,bounds.bottom+gap));
      const above=Math.max(0,Math.min(bottomLimit,bounds.top-gap)-topLimit);
      const upward=below<desired && above>below,available=upward?above:below;
      if(available<=0){menu.remove();return;}
      const height=Math.min(desired,available);
      menu.style.maxHeight=height+'px';
      menu.style.top=(upward?Math.min(bottomLimit,bounds.top-gap)-height:Math.max(topLimit,bounds.bottom+gap))+'px';
      const highlight=()=>{
        [...menu.children].forEach((row,i)=>{
          row.classList.toggle('is-active',i===active);
          row.setAttribute('aria-selected',String(i===active));
        });
        button.setAttribute('aria-activedescendant',menu.children[active].id);
        const row=menu.children[active];
        if(row.offsetTop<menu.scrollTop)menu.scrollTop=row.offsetTop;
        else if(row.offsetTop+row.offsetHeight>menu.scrollTop+menu.clientHeight)menu.scrollTop=row.offsetTop+row.offsetHeight-menu.clientHeight;
      };
      const choose=()=>{
        if(select.disabled || options[active]?.disabled){closeSelect();return;}
        const changed=select.selectedIndex!==active;select.selectedIndex=active;closeSelect();widget.sync();button.focus({preventScroll:true});
        if(changed){select.dispatchEvent(new Event('input',{bubbles:true}));select.dispatchEvent(new Event('change',{bubbles:true}));}
      };
      const outside=event=>{if(!menu.contains(event.target)&&!button.contains(event.target))closeSelect();};
      const scroll=event=>{
        if(menu.contains(event.target))return;
        const current=button.getBoundingClientRect();
        // A click may first scroll its trigger into view. Ignore that queued
        // event when the trigger is already at the position used to place us.
        if(Math.abs(current.top-bounds.top)>.5 || Math.abs(current.left-bounds.left)>.5)closeSelect();
      };
      const focus=event=>{if(event.target!==button&&!menu.contains(event.target))closeSelect();};
      document.addEventListener('pointerdown',outside,true);document.addEventListener('scroll',scroll,true);document.addEventListener('focusin',focus);window.addEventListener('resize',closeSelect);
      dropdown={select,button,menu,choose,release:()=>{document.removeEventListener('pointerdown',outside,true);document.removeEventListener('scroll',scroll,true);document.removeEventListener('focusin',focus);window.removeEventListener('resize',closeSelect);},
        move:key=>{const i=enabled.indexOf(active);active=key==='Home'?enabled[0]:key==='End'?enabled[enabled.length-1]:enabled[(i+(key==='ArrowDown'?1:enabled.length-1))%enabled.length];highlight();},
        search:key=>{const now=Date.now();searchText=now-searchTime>700?key:searchText+key;searchTime=now;const match=enabled.find(i=>options[i].textContent.toLocaleLowerCase().startsWith(searchText.toLocaleLowerCase()));if(match!==undefined){active=match;highlight();}}
      };
      menu.addEventListener('pointerdown',event=>event.preventDefault());
      menu.addEventListener('pointermove',event=>{const row=event.target.closest('[data-option-index]');if(row&&!options[Number(row.dataset.optionIndex)].disabled){active=Number(row.dataset.optionIndex);highlight();}});
      menu.addEventListener('click',event=>{const row=event.target.closest('[data-option-index]');if(row&&!options[Number(row.dataset.optionIndex)].disabled){active=Number(row.dataset.optionIndex);choose();}});
      button.setAttribute('aria-expanded','true');highlight();
    }
    const icon = path => `<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" aria-hidden="true">${path}</svg>`;
    const refreshIcon = icon('<path d="M20 7v5h-5M4 17v-5h5"/><path d="M6 7a7 7 0 0 1 12-1l2 3M4 15l2 3a7 7 0 0 0 12-1"/>');
    const searchIcon = icon('<circle cx="10.5" cy="10.5" r="6.5"/><path d="m16 16 5 5"/>');
    const params = () => new URLSearchParams({q:state.query,category:state.category === 'all' ? '' : state.category,source_project:state.source === 'all' ? '' : state.source,source_scope:state.sourceScope,status:state.status,page:state.page,page_size:20,intake_page:state.intakePage,intake_page_size:20,intake_q:state.intakeQuery,intake_category:state.intakeCategory,intake_source_project:state.intakeSource}).toString();
    const labels = () => Object.fromEntries((state.data?.categories || []).map(item => [item.id,item.label]));
    const canWrite = () => window.EvolvAuth?.canWrite === true;
    const total = name => Number(state.data?.[name + '_total'] ?? state.data?.[name]?.length ?? 0);
    const notify = message => {state.notice = message; state.error = false; updateNotice(); toast?.(message);};
    function clearCache() {cache.clear(); cacheBytes = 0;}
    function remember(key, data) {
      const size = JSON.stringify(data).length * 2;
      if (cache.has(key)) {cacheBytes -= cache.get(key).size; cache.delete(key);}
      if (size > MAX_CACHE_BYTES) return;
      cache.set(key, {data, size}); cacheBytes += size;
      while (cache.size > MAX_CACHE_ENTRIES || cacheBytes > MAX_CACHE_BYTES) {
        const oldest = cache.keys().next().value;
        cacheBytes -= cache.get(oldest).size; cache.delete(oldest);
      }
    }
    function cached(key) {
      const entry = cache.get(key);
      if (!entry) return null;
      cache.delete(key); cache.set(key, entry);
      return entry.data;
    }
    async function request(path, body, signal) {
      const response = await EvolvAuth.fetch(path, body === undefined ? {signal} : {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
      const result = await response.json();
      if (!response.ok || result.ok === false) {
        const messages = {revision_conflict:'这条资料已更新，请刷新后重新核对。',forbidden:'当前账号没有编辑权限。',service_busy:'正在处理其他资料，请稍后重试。'};
        throw new Error(messages[result.error] || result.error || '操作失败，请重试。');
      }
      return result;
    }
    function stopRead() {
      ++state.sequence;
      if (state.read) {clearTimeout(state.read.timer); state.read.controller.abort(); state.read = null;}
      state.loading = false;
    }
    async function load({force = false} = {}) {
      stopRead();
      const key = params(), requestId = state.sequence;
      state.needsLoad = true;
      const hit = !force && cached(key);
      if (hit) {
        state.data = hit; state.page = Number(hit.page) || 1; state.intakePage = Number(hit.intake_page) || 1;
        state.dataKey = params(); state.needsLoad = false;
        state.error = false; state.notice = ''; update(); return;
      }
      if (state.suspended) return;
      const controller = new AbortController();
      const read = {controller, timedOut:false, timer:null};
      read.timer = setTimeout(() => {read.timedOut = true; controller.abort();}, 15000);
      state.read = read; state.loading = true; state.error = false; state.notice = '';
      update();
      try {
        const data = await request('/api/capabilities?' + key, undefined, controller.signal);
        if (requestId !== state.sequence || state.suspended) return;
        state.data = data; state.page = Number(data.page) || 1; state.intakePage = Number(data.intake_page) || 1;
        state.dataKey = params();
        // The server may clamp a page after deletions. Cache its canonical
        // page so the next/previous controls always use the displayed page.
        remember(state.dataKey, data);
        state.needsLoad = false;
      } catch (error) {
        if (requestId !== state.sequence || state.suspended) return;
        state.error = true;
        state.notice = read.timedOut ? '读取超时，请重试。' : '读取失败：' + error.message;
        if (state.data) state.notice += ' 已保留上次读取的结果。';
      } finally {
        clearTimeout(read.timer);
        if (requestId === state.sequence) {state.read = null; state.loading = false; update();}
      }
    }
    function mountMemoryModules(card) {
      const host=card.querySelector('[data-memory-modules]');
      if(!host || host.dataset.mounted || !window.EvolvMemModules?.mountMemory)return;
      host.dataset.mounted='true';
      Promise.resolve().then(()=>window.EvolvMemModules.mountMemory(host,Number(card.dataset.memoryCard))).catch(()=>{
        host.dataset.mounted='';host.textContent='模块关联暂未加载，重新展开可重试。';
      });
    }
    function memoryCard(item) {
      const id = String(item.memory_id), expanded = state.expanded.has(id), categoryLabels = labels();
      const facts=item.governance || {}, projects=item.current_source_projects || item.source_projects || [];
      const values={reusable:'可复用',reference:'参考资料',review:'待核对',ignore:'信息不足',stale:'需重新核对',unassessed:'尚未评估'};
      const sourceLabels={current:'当前来源有效',partial:'部分来源失效',invalid:'来源已失效',untraceable:'来源待追溯'};
      const statusLabels={active:'已整理',pending:'待核对',ignored:'已停用',archived:'历史记录'};
      const ownership=projects.length?projects.join('、'):facts.ownership_state==='global'?'通用资料（无项目归属）':'项目归属待确认';
      return `<article class="memory-card ${expanded ? 'is-expanded' : ''}" data-memory-card="${esc(id)}" data-memory="${esc(id)}" tabindex="0" aria-label="${esc(item.title)}" aria-expanded="${expanded}">
        <div class="meta memory-card-meta"><span class="tag memory-category">${esc(categoryLabels[item.category] || item.category)}</span>${(item.keywords || []).map(keyword => `<span class="tag">${esc(keyword)}</span>`).join('')}<span class="tag capability-record-status">${esc(statusLabels[item.status] || '已保存')}</span></div>
        <h3>${esc(item.title)}</h3><p class="memory-content memory-body">${esc(item.content)}</p>
        <dl class="capability-governance"><div><dt>有用性</dt><dd>${esc(values[facts.value] || '尚未评估')}</dd></div><div><dt>方法结果</dt><dd>未独立验证</dd></div><div><dt>来源状态</dt><dd class="capability-source-state is-${esc(facts.source_state || 'untraceable')}">${esc(sourceLabels[facts.source_state] || '来源待追溯')}</dd></div></dl>
        <div class="memory-card-footer"><span>当前项目：${esc(ownership)}</span><button class="memory-expand btn small" type="button" data-memory-toggle aria-expanded="${expanded}" aria-controls="memory-source-${esc(id)}">${expanded ? '收起内容' : '展开全文与来源'}</button></div>
        <div id="memory-source-${esc(id)}" class="source-detail" data-memory-source-detail ${expanded ? '' : 'hidden'}><div class="capability-source-reasons"><strong>核对依据</strong><ul>${(facts.reasons?.length?facts.reasons:['来源和价值状态尚未完成核对。']).map(reason=>`<li>${esc(reason)}</li>`).join('')}</ul></div><div>原始来源：${esc(item.source_ref || '未记录')}</div>${item.source_project&&!projects.includes(item.source_project)?`<div>历史记录项目：${esc(item.source_project)}</div>`:''}<div>记忆编号：${esc(id)}</div><div data-memory-modules></div></div>
      </article>`;
    }
    function skeleton() {
      return Array.from({length:6}, () => '<div class="memory-card capability-skeleton" aria-hidden="true"><span class="skeleton-line short"></span><span class="skeleton-line"></span><span class="skeleton-line"></span><span class="skeleton-line short"></span></div>').join('');
    }
    function intakeCards() {
      return (state.data?.intake || []).map(item => {
        const id = String(item.intake_id), categories = state.data.categories || [];
        const selected = state.intakeChoices.get(id) || (categories.some(c => c.id === item.suggested_category) ? item.suggested_category : categories[0]?.id || '');
        return `<article class="memory-card" data-intake-card="${esc(id)}"><h3>${esc(item.title)}</h3><p class="intake-content">${esc(item.content)}</p><div class="meta">来源项目：${esc(item.source_project || '未记录')} · 建议：${esc(labels()[item.suggested_category] || item.suggested_category || '无')}</div><div class="toolbar"><div class="intake-category-label"><span>归入分类</span><select class="select" data-intake-category="${esc(id)}" aria-label="${esc(item.title)}的分类">${categories.map(category => `<option value="${esc(category.id)}" ${selected === category.id ? 'selected' : ''}>${esc(category.label)}</option>`).join('')}</select></div><button class="btn primary small" type="button" data-assign="${esc(id)}" data-revision="${esc(item.revision)}">确认归类</button></div></article>`;
      }).join('') || `<div class="empty capability-empty">${state.intakeQuery||state.intakeCategory||state.intakeSource?'没有符合筛选条件的待分类资料。':'待分类已清空。'}</div>`;
    }
    function suggestionCards() {
      return (state.data?.suggestions || []).map(item => `<article class="memory-card"><h3>${esc(item.label)}</h3><p class="intake-content">${esc(item.reason)}</p><div class="toolbar"><button class="btn primary small" type="button" data-suggestion="accept" data-id="${esc(item.suggestion_id)}">接受并创建</button><button class="btn small" type="button" data-suggestion="reject" data-id="${esc(item.suggestion_id)}">拒绝</button></div></article>`).join('') || '<div class="empty capability-empty">暂无新增分类建议。</div>';
    }
    function updateNotice() {
      const notice = $('[data-capability-notice]');
      notice.hidden = !state.notice;
      notice.classList.toggle('is-error', state.error);
      $('[data-notice-text]').textContent = state.notice;
      $('[data-memory-retry]').hidden = !state.error;
      $('[data-show-pending]').hidden = !state.assignedPending || state.error;
    }
    function updateWriteControls() {
      $('[data-capability-readonly]').hidden = canWrite();
      mount.querySelectorAll('[data-assign],[data-suggestion]').forEach(button => {
        button.disabled = !canWrite() || state.writing || state.loading;
        button.setAttribute('aria-disabled', String(button.disabled));
        button.title = !canWrite() ? '当前为只读访问' : '';
      });
      mount.querySelectorAll('[data-intake-category]').forEach(select => {select.disabled = !canWrite() || state.writing || state.loading;});
      enhanceSelects();
    }
    function update() {
      closeSelect();
      const data = state.data;
      mount.setAttribute('aria-busy', String(state.loading));
      $('[data-memory-refresh]').disabled = state.loading;
      $('[data-memory-refresh]').setAttribute('aria-label', state.loading ? '正在刷新记忆' : '刷新记忆');
      updateNotice();
      mount.querySelectorAll('[data-tab]').forEach(button => {
        const active = button.dataset.tab === state.tab;
        button.classList.toggle('active', active);
        button.setAttribute('aria-selected', String(active));
        button.tabIndex = active ? 0 : -1;
      });
      $('[data-intake-count]').textContent = data ? Number(data.intake_all_total ?? total('intake')) : '—';
      $('[data-suggestions-count]').textContent = data ? total('suggestions') : '—';
      $('[data-pending-count]').textContent = data ? Number(data.pending_total || 0) : '—';
      mount.querySelectorAll('[data-memory-status-filter]').forEach(button=>{const active=button.dataset.memoryStatusFilter===state.status;button.classList.toggle('active',active);button.setAttribute('aria-pressed',String(active));});
      $('[data-source-history]').checked=state.sourceScope==='historical';
      const categories = data?.categories || [], counts = data?.counts || {};
      const categoryMarkup = `<button class="chip capability-category ${state.category === 'all' ? 'active' : ''}" type="button" data-category="all" aria-pressed="${state.category === 'all'}"><span>全部能力</span><b>${Object.values(counts).reduce((sum,count) => sum + Number(count || 0),0)}</b></button>${categories.map(item => `<button class="chip capability-category ${state.category === item.id ? 'active' : ''}" type="button" data-category="${esc(item.id)}" aria-pressed="${state.category === item.id}"><span>${esc(item.label)}</span><b>${Number(counts[item.id]) || 0}</b></button>`).join('')}`;
      if ($('[data-category-list]').innerHTML !== categoryMarkup) $('[data-category-list]').innerHTML = categoryMarkup;
      const sources = [...new Set([...(data?.source_projects || []), ...(data?.items || []).map(item => item.source_project), ...(state.source === 'all' ? [] : [state.source])])].filter(value => typeof value === 'string' && value);
      const sourceMarkup = '<option value="all">全部项目</option>' + sources.map(source => `<option value="${esc(source)}" ${state.source === source ? 'selected' : ''}>${esc(source)}</option>`).join('');
      if ($('[data-memory-source]').innerHTML !== sourceMarkup) $('[data-memory-source]').innerHTML = sourceMarkup;
      $('[data-memory-source]').value = state.source;
      // The input is a draft, separate from the last submitted query. Responses
      // never restore its value or replace the input node.
      $('[data-memory-clear]').disabled = !state.draft && !state.query && state.source === 'all' && state.sourceScope === 'current' && state.category === 'all';
      $('[data-formal-tools]').hidden = state.tab !== 'formal';
      $('[data-memory-status-filters]').hidden = state.tab !== 'formal';
      $('[data-intake-tools]').hidden = state.tab !== 'intake';
      $('.capability-sidebar').hidden = state.tab !== 'formal';
      $('.capability-layout').classList.toggle('is-queue',state.tab !== 'formal');
      const intakeFilterMarkup='<option value="">全部建议分类</option>'+categories.map(item=>`<option value="${esc(item.id)}">${esc(item.label)}</option>`).join('');
      if($('[data-intake-filter]').innerHTML!==intakeFilterMarkup)$('[data-intake-filter]').innerHTML=intakeFilterMarkup;
      $('[data-intake-filter]').value=state.intakeCategory;
      const intakeSources=[...new Set([...(data?.intake_source_projects||[]),...(data?.intake||[]).map(item=>item.source_project),state.intakeSource])].filter(Boolean);
      const intakeSourceMarkup='<option value="">全部来源项目</option>'+intakeSources.map(source=>`<option value="${esc(source)}">${esc(source)}</option>`).join('');
      if($('[data-intake-source]').innerHTML!==intakeSourceMarkup)$('[data-intake-source]').innerHTML=intakeSourceMarkup;
      $('[data-intake-source]').value=state.intakeSource;
      $('[data-intake-clear]').disabled=!state.intakeDraft&&!state.intakeQuery&&!state.intakeCategory&&!state.intakeSource;
      mount.querySelectorAll('[data-capability-panel]').forEach(panel => {panel.hidden = panel.dataset.capabilityPanel !== state.tab;});
      const currentLabel = labels()[state.category] || '全部能力';
      $('[data-result-title]').textContent = state.tab === 'formal' ? currentLabel : state.tab === 'intake' ? '待分类暂存' : '新增分类建议';
      $('[data-result-count]').textContent = state.loading ? (data ? '正在读取，暂时显示上次结果…' : '正在读取记忆…') : state.error ? '读取未完成' : state.tab === 'formal' ? `共 ${data?.total || 0} 条记忆` : `共 ${total(state.tab)} 条`;
      // Render only the visible lane. Queue rows can be numerous and should
      // not delay the first screen of formal memories or every filter change.
      const panelKey = data || (state.loading ? 'loading' : 'empty');
      if (renderedPanels.get(state.tab) !== panelKey) {
        const target = state.tab === 'formal' ? $('[data-memory-results]') : state.tab === 'intake' ? $('[data-intake-results]') : $('[data-suggestion-results]');
        target.innerHTML = !data ? (state.loading ? skeleton() : '<div class="empty capability-empty">暂时无法读取记忆，请点击上方“重试”。</div>') : state.tab === 'formal' ? ((data.items || []).map(memoryCard).join('') || '<div class="empty capability-empty">没有符合条件的记忆，请调整筛选。</div>') : state.tab === 'intake' ? intakeCards() : suggestionCards();
        renderedPanels.set(state.tab, panelKey);
      }
      const page = Number(data?.page) || 1, pages = Math.max(1, Number(data?.pages) || 1);
      $('[data-page-status]').textContent = `第 ${page} / ${pages} 页 · 共 ${data?.total || 0} 条`;
      $('[data-page="prev"]').disabled = !data || state.loading || state.error || page <= 1;
      $('[data-page="next"]').disabled = !data || state.loading || state.error || page >= pages;
      const intakePage=Number(data?.intake_page)||1,intakePages=Math.max(1,Number(data?.intake_pages)||1);
      $('[data-intake-page-status]').textContent=`第 ${intakePage} / ${intakePages} 页 · 共 ${total('intake')} 条`;
      $('[data-intake-page="prev"]').disabled=!data||state.loading||state.error||intakePage<=1;
      $('[data-intake-page="next"]').disabled=!data||state.loading||state.error||intakePage>=intakePages;
      updateWriteControls();
      mount.querySelectorAll('[data-memory-card][aria-expanded="true"]').forEach(mountMemoryModules);
    }
    function toggleCard(card) {
      const id = card.dataset.memoryCard, expanded = !state.expanded.has(id);
      if (expanded) {
        state.expanded.add(id);
        if (state.expanded.size > 400) state.expanded.delete(state.expanded.values().next().value);
      } else state.expanded.delete(id);
      card.classList.toggle('is-expanded', expanded);
      card.setAttribute('aria-expanded', String(expanded));
      const button = card.querySelector('[data-memory-toggle]');
      button.setAttribute('aria-expanded', String(expanded));
      button.textContent = expanded ? '收起内容' : '展开全文与来源';
      card.querySelector('[data-memory-source-detail]').hidden = !expanded;
      if(expanded)mountMemoryModules(card);
    }
    function selectTab(tab, focus = false) {
      if (!['formal','intake','suggestions'].includes(tab)) return;
      state.tab = tab; update(); onTab?.(tab);
      if (focus) $(`[data-tab="${tab}"]`).focus();
    }
    async function write(button) {
      if (state.writing) return;
      if (!canWrite()) {notify('当前账号没有编辑权限。'); updateWriteControls(); return;}
      const intake = button.hasAttribute('data-assign'), id = intake ? button.dataset.assign : button.dataset.id;
      const category = intake ? mount.querySelector(`[data-intake-category="${CSS.escape(id)}"]`)?.value : '';
      if (intake && !category) {notify('请选择要归入的分类。'); return;}
      state.writing = true; updateWriteControls();
      const originalLabel = button.textContent;
      button.textContent = '正在提交…';
      try {
        const path = intake ? `/api/capabilities/intake/${encodeURIComponent(id)}/assign` : `/api/capability-categories/suggestions/${encodeURIComponent(id)}/${button.dataset.suggestion}`;
        const result=await request(path, intake ? {category,expected_revision:Number(button.dataset.revision)} : {});
        if(intake)state.assignedPending=result.memory_status==='pending';
        if (intake) state.intakeChoices.delete(id);
        clearCache(); state.needsLoad = true;
        const message = intake ? (result.notice || '分类已保存，请核对资料状态。') : button.dataset.suggestion === 'accept' ? '已接受建议并创建新分类' : '已拒绝分类建议';
        // Keep the tab the user is currently browsing, even when this write
        // finishes after they have navigated elsewhere.
        if (!state.suspended) {await load({force:true}); if (!state.error) notify(message); else toast?.(message);}
        else notify(message);
      } catch (error) {
        notify(error.message || '提交结果未确认，请刷新核对后再操作。');
      } finally {
        state.writing = false;
        button.textContent = originalLabel;
        updateWriteControls();
      }
    }
    mount.classList.add('capability-workspace');
    mount.innerHTML = `<header class="capability-heading"><div><span class="capability-eyebrow">按技术分类浏览</span><h1>经验与能力库</h1><p class="capability-description">按前端、后端、测试等分类查找记忆与来源；方法的验证状态可到<a href="#experiences">经验案例库</a>查看。</p></div><button class="btn" type="button" data-memory-refresh>${refreshIcon}<span>刷新</span></button></header>
      <div class="capability-tabs" role="tablist" aria-label="记忆处理状态"><button class="chip active" type="button" role="tab" id="capability-tab-formal" aria-controls="capability-panel-formal" aria-selected="true" data-tab="formal">能力资料</button><button class="chip" type="button" role="tab" id="capability-tab-intake" aria-controls="capability-panel-intake" aria-selected="false" tabindex="-1" data-tab="intake">待分类 <b data-intake-count>0</b></button><button class="chip" type="button" role="tab" id="capability-tab-suggestions" aria-controls="capability-panel-suggestions" aria-selected="false" tabindex="-1" data-tab="suggestions">分类建议 <b data-suggestions-count>0</b></button></div>
      <div class="capability-layout"><aside class="capability-sidebar" aria-label="能力分类"><h2>能力分类</h2><div data-category-list></div></aside><div class="capability-main">
      <form class="toolbar capability-toolbar" data-formal-tools role="search" aria-label="搜索开发能力记忆"><div class="capability-search">${searchIcon}<input class="input" type="search" data-memory-query aria-label="搜索记忆、技术栈或来源项目" placeholder="搜索记忆、技术栈或来源项目" autocomplete="off"></div><select class="select" data-memory-source aria-label="筛选项目"><option value="all">全部项目</option></select><label class="capability-source-mode"><input type="checkbox" data-source-history>按历史项目筛选</label><button class="btn primary" type="submit" data-memory-search>搜索</button><button class="btn" type="button" data-memory-clear>清空筛选</button></form>
      <div class="capability-status-filters" data-memory-status-filters role="group" aria-label="资料状态"><button class="chip active" type="button" data-memory-status-filter="active" aria-pressed="true">已整理</button><button class="chip" type="button" data-memory-status-filter="pending" aria-pressed="false">待核对 <b data-pending-count>0</b></button><button class="chip" type="button" data-memory-status-filter="all" aria-pressed="false">全部记录</button></div>
      <form class="toolbar capability-toolbar" data-intake-tools role="search" aria-label="搜索待分类资料" hidden><div class="capability-search">${searchIcon}<input class="input" type="search" data-intake-query aria-label="搜索待分类资料" placeholder="搜索待分类资料" autocomplete="off"></div><select class="select" data-intake-filter aria-label="建议分类"><option value="">全部建议分类</option></select><select class="select" data-intake-source aria-label="待分类来源项目"><option value="">全部来源项目</option></select><button class="btn primary" type="submit">搜索</button><button class="btn" type="button" data-intake-clear>清空筛选</button></form>
      <div class="capability-notice refresh-notice" data-capability-notice role="status" hidden><span data-notice-text></span><button class="btn small" type="button" data-show-pending hidden>查看全部待核对</button><button class="btn small" type="button" data-memory-retry hidden>重试</button></div><div class="capability-readonly" data-capability-readonly hidden>当前为只读访问</div>
      <div class="capability-result-summary"><h2 data-result-title>全部能力</h2><span data-result-count role="status" aria-live="polite">正在读取记忆…</span></div>
      <div class="capability-content"><section id="capability-panel-formal" role="tabpanel" aria-labelledby="capability-tab-formal" data-capability-panel="formal"><div class="memory-grid" data-memory-results></div><div class="pager"><button class="btn small" type="button" data-page="prev" disabled>上一页</button><span data-page-status></span><button class="btn small" type="button" data-page="next" disabled>下一页</button></div></section><section id="capability-panel-intake" role="tabpanel" aria-labelledby="capability-tab-intake" data-capability-panel="intake" hidden><div class="memory-grid" data-intake-results></div><div class="pager"><button class="btn small" type="button" data-intake-page="prev" disabled>上一页</button><span data-intake-page-status></span><button class="btn small" type="button" data-intake-page="next" disabled>下一页</button></div></section><section id="capability-panel-suggestions" role="tabpanel" aria-labelledby="capability-tab-suggestions" data-capability-panel="suggestions" hidden><div class="memory-grid" data-suggestion-results></div></section></div></div></div>`;
    mount.addEventListener('input', event => {
      if(event.target.matches('[data-intake-query]')){state.intakeDraft=event.target.value;$('[data-intake-clear]').disabled=!state.intakeDraft&&!state.intakeQuery&&!state.intakeCategory&&!state.intakeSource;}
      if (event.target.matches('[data-memory-query]')) {state.draft = event.target.value; $('[data-memory-clear]').disabled = !state.draft && !state.query && state.source === 'all' && state.sourceScope === 'current' && state.category === 'all';}
    });
    mount.addEventListener('submit', event => {
      if(event.target.matches('[data-intake-tools]')){event.preventDefault();state.intakeQuery=state.intakeDraft;state.intakePage=1;load();return;}
      if (!event.target.matches('[data-formal-tools]')) return;
      event.preventDefault(); state.query = state.draft; state.page = 1; load();
    });
    mount.addEventListener('change', event => {
      if(event.target.matches('[data-source-history]')){state.sourceScope=event.target.checked?'historical':'current';state.page=1;load();}
      if(event.target.matches('[data-intake-filter],[data-intake-source]')){state[event.target.hasAttribute('data-intake-filter')?'intakeCategory':'intakeSource']=event.target.value;state.intakePage=1;load();}
      if (event.target.matches('[data-memory-source]')) {state.source = event.target.value; state.page = 1; load();}
      if (event.target.matches('[data-intake-category]')) {
        state.intakeChoices.set(event.target.dataset.intakeCategory, event.target.value);
        if (state.intakeChoices.size > 400) state.intakeChoices.delete(state.intakeChoices.keys().next().value);
      }
    });
    mount.addEventListener('click', event => {
      const button = event.target.closest('button');
      if (button?.disabled) return;
      if(button?.hasAttribute('data-memory-status-filter')){state.status=button.dataset.memoryStatusFilter;state.page=1;load();return;}
      if(button?.hasAttribute('data-show-pending')){state.status='pending';state.page=1;state.query='';state.draft='';state.source='all';state.sourceScope='current';state.category='all';$('[data-memory-query]').value='';selectTab('formal');load();return;}
      if (button?.hasAttribute('data-tab')) {selectTab(button.dataset.tab); return;}
      if (button?.hasAttribute('data-category')) {state.category = button.dataset.category; state.page = 1; selectTab('formal'); load(); return;}
      if(button?.hasAttribute('data-intake-page')){state.intakePage+=button.dataset.intakePage==='next'?1:-1;load();return;}
      if(button?.hasAttribute('data-intake-clear')){state.intakeQuery='';state.intakeDraft='';state.intakeCategory='';state.intakeSource='';state.intakePage=1;$('[data-intake-query]').value='';load();$('[data-intake-query]').focus();return;}
      if (button?.hasAttribute('data-memory-refresh') || button?.hasAttribute('data-memory-retry')) {load({force:true}); return;}
      if (button?.hasAttribute('data-memory-clear')) {state.query = ''; state.draft = ''; state.source = 'all'; state.sourceScope='current'; state.category = 'all'; state.page = 1; $('[data-memory-query]').value = ''; load(); $('[data-memory-query]').focus(); return;}
      if (button?.hasAttribute('data-page')) {state.page += button.dataset.page === 'next' ? 1 : -1; load(); return;}
      if (button?.hasAttribute('data-assign') || button?.hasAttribute('data-suggestion')) {write(button); return;}
      const card = event.target.closest('[data-memory-card]');
      if (card && (!button || button.hasAttribute('data-memory-toggle')) && !event.target.closest('a,input,select,textarea') && !window.getSelection()?.toString()) toggleCard(card);
    });
    mount.addEventListener('keydown', event => {
      const tab = event.target.closest('[data-tab]');
      if (tab && ['ArrowLeft','ArrowRight','Home','End'].includes(event.key)) {
        event.preventDefault(); const tabs = ['formal','intake','suggestions'], index = tabs.indexOf(state.tab);
        selectTab(event.key === 'Home' ? tabs[0] : event.key === 'End' ? tabs[2] : tabs[(index + (event.key === 'ArrowRight' ? 1 : 2)) % 3], true); return;
      }
      if (event.target.matches('[data-memory-card]') && (event.key === 'Enter' || event.key === ' ')) {event.preventDefault(); toggleCard(event.target);}
    });
    const handle = {
      suspend() {closeSelect(); state.suspended = true; if (state.loading) state.needsLoad = true; stopRead(); update();},
      resume(tab) {state.suspended = false; if(['formal','intake','suggestions'].includes(tab))state.tab=tab;update();if (state.needsLoad) load();},
      refresh() {return load({force:true});},
    };
    controllers.set(mount, handle);
    load();
    return handle;
  }};
})();
