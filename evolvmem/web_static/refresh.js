(() => {
  'use strict';
  /* Tabler Icons Outline — https://github.com/tabler/tabler-icons
   * Pinned commit: ed177a085b652fe04ec4f37ced867c94621e7840
   * MIT License
   *
   * Copyright (c) 2020-2026 Paweł Kuna
   *
   * Permission is hereby granted, free of charge, to any person obtaining a copy
   * of this software and associated documentation files (the "Software"), to deal
   * in the Software without restriction, including without limitation the rights
   * to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
   * copies of the Software, and to permit persons to whom the Software is
   * furnished to do so, subject to the following conditions:
   *
   * The above copyright notice and this permission notice shall be included in all
   * copies or substantial portions of the Software.
   *
   * THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
   * IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
   * FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
   * AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
   * LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
   * OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
   * SOFTWARE.
   */
  let navigation=null;
  function initPreview(root){
    const header=root.querySelector('.refresh-topnav'),nav=header?.querySelector('.signal-nav');
    if(!nav)return false;
    if(header.dataset.navigationReady)return true;
    header.dataset.navigationReady='true';
    const main=root.querySelector('main.refresh-main');
    if(main&&!root.querySelector('.refresh-skip-link')){
      if(!main.id)main.id='main-content';
      if(!main.hasAttribute('tabindex'))main.tabIndex=-1;
      const skip=document.createElement('a');skip.className='refresh-skip-link';skip.href='#'+main.id;skip.textContent='跳过导航';
      skip.addEventListener('click',event=>{event.preventDefault();main.focus({preventScroll:true});main.scrollIntoView({block:'start',behavior:'instant'});});header.before(skip);
    }
    const icons={"home": "<path d=\"M5 4h4a1 1 0 0 1 1 1v6a1 1 0 0 1 -1 1h-4a1 1 0 0 1 -1 -1v-6a1 1 0 0 1 1 -1\" /><path d=\"M5 16h4a1 1 0 0 1 1 1v2a1 1 0 0 1 -1 1h-4a1 1 0 0 1 -1 -1v-2a1 1 0 0 1 1 -1\" /><path d=\"M15 12h4a1 1 0 0 1 1 1v6a1 1 0 0 1 -1 1h-4a1 1 0 0 1 -1 -1v-6a1 1 0 0 1 1 -1\" /><path d=\"M15 4h4a1 1 0 0 1 1 1v2a1 1 0 0 1 -1 1h-4a1 1 0 0 1 -1 -1v-2a1 1 0 0 1 1 -1\" />", "archives": "<path d=\"M5 4h4l3 3h7a2 2 0 0 1 2 2v8a2 2 0 0 1 -2 2h-14a2 2 0 0 1 -2 -2v-11a2 2 0 0 1 2 -2\" />", "capabilities": "<path d=\"M3 19a9 9 0 0 1 9 0a9 9 0 0 1 9 0\" /><path d=\"M3 6a9 9 0 0 1 9 0a9 9 0 0 1 9 0\" /><path d=\"M3 6l0 13\" /><path d=\"M12 6l0 13\" /><path d=\"M21 6l0 13\" />", "organization": "<path d=\"M3.5 5.5l1.5 1.5l2.5 -2.5\" /><path d=\"M3.5 11.5l1.5 1.5l2.5 -2.5\" /><path d=\"M3.5 17.5l1.5 1.5l2.5 -2.5\" /><path d=\"M11 6l9 0\" /><path d=\"M11 12l9 0\" /><path d=\"M11 18l9 0\" />", "progress": "<path d=\"M12 8l0 4l2 2\" /><path d=\"M3.05 11a9 9 0 1 1 .5 4m-.5 5v-5h5\" />", "principles": "<path d=\"M3 17a2 2 0 0 1 2 -2h2a2 2 0 0 1 2 2v2a2 2 0 0 1 -2 2h-2a2 2 0 0 1 -2 -2l0 -2\" /><path d=\"M15 17a2 2 0 0 1 2 -2h2a2 2 0 0 1 2 2v2a2 2 0 0 1 -2 2h-2a2 2 0 0 1 -2 -2l0 -2\" /><path d=\"M9 5a2 2 0 0 1 2 -2h2a2 2 0 0 1 2 2v2a2 2 0 0 1 -2 2h-2a2 2 0 0 1 -2 -2l0 -2\" /><path d=\"M6 15v-1a2 2 0 0 1 2 -2h8a2 2 0 0 1 2 2v1\" /><path d=\"M12 9l0 3\" />", "account": "<path d=\"M3 12a9 9 0 1 0 18 0a9 9 0 1 0 -18 0\" /><path d=\"M9 10a3 3 0 1 0 6 0a3 3 0 1 0 -6 0\" /><path d=\"M6.168 18.849a4 4 0 0 1 3.832 -2.849h4a4 4 0 0 1 3.834 2.855\" />", "logout": "<path d=\"M14 8v-2a2 2 0 0 0 -2 -2h-7a2 2 0 0 0 -2 2v12a2 2 0 0 0 2 2h7a2 2 0 0 0 2 -2v-2\" /><path d=\"M9 12h12l-3 -3\" /><path d=\"M18 15l3 -3\" />"};
    const icon=key=>`<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${icons[key]}</svg>`;
    const groups=[
      {id:'home',label:'总览',icon:'home',pages:[['home','总览']]},
      {id:'project',label:'项目库',icon:'archives',pages:[['archives','项目档案'],['projects','旧版资料']]},
      {id:'experience',label:'经验库',icon:'capabilities',pages:[['capabilities','分类经验'],['experiences','经验案例'],['qa','旧版问答']]},
      {id:'organize',label:'资料整理',icon:'organization',pages:[['organization','自动整理'],['cleaning','资料清洗'],['intake','来源资料'],['skill','规则设置']]},
      {id:'task',label:'任务续接',icon:'progress',pages:[['progress','任务续接']]},
      {id:'principle',label:'工作原理',icon:'principles',pages:[['principles','工作原理']]}
    ];
    const single=new Set(['home','experiences','progress','principles']);
    const route=key=>single.has(key)?'#'+key:'#knowledge/'+key;
    const decode=hash=>{
      const [path,query='']=String(hash||'#home').slice(1).split('?');const params=new URLSearchParams(query);
      const key=path.startsWith('knowledge/')?path.split('/')[1]:path==='memories'?'projects':path||'home';
      const scoped=params.get('project')&&!['__all__','__global__'].includes(params.get('project'));
      let section=key;
      if(key==='unassigned'||key==='library'&&scoped)section='projects';
      else if(key==='library')section='intake';
      else if(['rules','learning'].includes(key))section='skill';
      const group=groups.find(group=>group.pages.some(([id])=>id===section))||groups[0];
      return {key,section,group,hash:route(key)+(query?'?'+query:'')};
    };
    const setDestination=(anchor,hash)=>{
      const {key}=decode(hash);anchor.href=hash;anchor.dataset.navShell='true';
      anchor.dataset.pageLink=single.has(key)?key:'memories';
      if(single.has(key))delete anchor.dataset.knowledgeView;else anchor.dataset.knowledgeView=key;
    };
    const makeLink=(key,label,className)=>{const link=document.createElement('a');link.className=className;link.textContent=label;setDestination(link,route(key));return link;};
    nav.replaceChildren();nav.setAttribute('aria-label','主导航');
    for(const group of groups){
      const link=makeLink(group.pages[0][0],group.label,'nav-primary-link');link.dataset.navGroup=group.id;
      link.innerHTML=`<span class="nav-line-icon">${icon(group.icon)}</span><span class="nav-label"></span>`;
      link.querySelector('.nav-label').textContent=group.label;nav.append(link);
    }
    const secondary=document.createElement('div');secondary.className='nav-secondary';secondary.hidden=true;
    const tabs=document.createElement('nav');tabs.className='nav-section-links';tabs.setAttribute('aria-label','栏目内导航');
    const scope=document.createElement('nav');scope.className='nav-material-scope';scope.setAttribute('aria-label','资料范围');scope.hidden=true;
    scope.append(makeLink('intake','待确认','nav-scope-link'),makeLink('library','全部资料','nav-scope-link'));
    secondary.append(tabs,scope);header.append(secondary);
    let currentGroup=null;
    const remember=hash=>{const current=decode(hash);const link=nav.querySelector(`[data-nav-group="${current.group.id}"]`);if(link)setDestination(link,current.hash);};
    const sync=hash=>{
      const current=decode(hash);remember(hash);
      nav.querySelectorAll('a').forEach(link=>{const active=link.dataset.navGroup===current.group.id;link.classList.toggle('active',active);link.setAttribute('aria-current',active?'page':'false');});
      secondary.hidden=current.group.pages.length===1;
      if(currentGroup!==current.group.id){tabs.replaceChildren(...current.group.pages.map(([key,label])=>{const link=makeLink(key,label,'nav-secondary-link');link.dataset.navSection=key;return link;}));currentGroup=current.group.id;}
      tabs.querySelectorAll('a').forEach(link=>{const active=link.dataset.navSection===current.section;link.classList.toggle('active',active);link.setAttribute('aria-current',active?'page':'false');});
      scope.hidden=current.section!=='intake';
      scope.querySelectorAll('a').forEach(link=>{const active=link.dataset.knowledgeView===current.key;link.classList.toggle('active',active);link.setAttribute('aria-current',active?'page':'false');});
      measureHeader();
    };
    const brand=header.querySelector('.signal-brand');if(brand){brand.href='#home';brand.dataset.pageLink='home';brand.title='EvolvMem 首页';}
    const account=header.querySelector('[data-auth-status]');
    const identity=account?.querySelector(':scope > span'),logout=account?.querySelector(':scope > button');
    if(identity && logout && !account.hidden){
      const toggle=document.createElement('button');toggle.type='button';toggle.className='nav-account-toggle';
      toggle.innerHTML=icon('account');
      toggle.title=identity.textContent;toggle.setAttribute('aria-label','当前账号：'+identity.textContent);
      toggle.setAttribute('aria-expanded','false');toggle.setAttribute('aria-controls','nav-account-panel');
      const panel=document.createElement('div');panel.id='nav-account-panel';panel.className='nav-account-panel';panel.hidden=true;
      panel.setAttribute('role','region');panel.setAttribute('aria-label','当前账号');panel.append(identity);document.body.append(panel);
      account.prepend(toggle);
      // Decorate the existing button; auth.js still owns CSRF, logout and errors.
      logout.classList.add('nav-logout');
      logout.innerHTML=icon('logout')+'<span class="nav-sr-only">退出登录</span>';
      let openedBounds=null;
      const close=()=>{openedBounds=null;panel.hidden=true;toggle.setAttribute('aria-expanded','false');};
      toggle.addEventListener('click',()=>{
        if(!panel.hidden){close();return;}
        panel.hidden=false;toggle.setAttribute('aria-expanded','true');
        const box=toggle.getBoundingClientRect();openedBounds=box;
        panel.style.left=Math.max(12,Math.min(innerWidth-panel.offsetWidth-12,box.right-panel.offsetWidth))+'px';
        panel.style.top=Math.min(innerHeight-panel.offsetHeight-12,box.bottom+8)+'px';
      });
      document.addEventListener('pointerdown',event=>{if(!panel.contains(event.target)&&!toggle.contains(event.target))close();});
      document.addEventListener('focusin',event=>{if(!panel.contains(event.target)&&!toggle.contains(event.target))close();});
      document.addEventListener('keydown',event=>{if(event.key==='Escape'&&!panel.hidden){close();toggle.focus();}});
      document.addEventListener('scroll',()=>{
        if(panel.hidden||!openedBounds)return;
        const current=toggle.getBoundingClientRect();
        if(Math.abs(current.top-openedBounds.top)>.5||Math.abs(current.left-openedBounds.left)>.5)close();
      },true);
      window.addEventListener('resize',close);window.addEventListener('hashchange',close);
    }
    document.body.dataset.navDesign='refined';
    const reveal=(container,link)=>{if(!link||container.scrollWidth<=container.clientWidth)return;const outer=container.getBoundingClientRect(),inner=link.getBoundingClientRect();if(inner.left<outer.left+6)container.scrollLeft-=outer.left-inner.left+6;else if(inner.right>outer.right-6)container.scrollLeft+=inner.right-outer.right+6;};
    for(const container of [nav,tabs,scope])container.addEventListener('focusin',event=>reveal(container,event.target.closest('a')));
    function measureHeader(){const height=Math.ceil(header.getBoundingClientRect().height);document.documentElement.style.setProperty('--nav-header-height',height+'px');document.documentElement.style.scrollPaddingTop=(height+16)+'px';reveal(tabs,tabs.querySelector('[aria-current="page"]'));}
    new ResizeObserver(measureHeader).observe(header);
    navigation={sync,remember};sync(location.hash);return true;
  }

  window.EvolvRefresh = {
    rememberNavigation(hash) { navigation?.remember(hash); },
    syncNavigation(hash) { navigation?.sync(hash); },
    initNavigation(root) {
      if(initPreview(root))return;
      const toggle = root.querySelector('[data-more-toggle]');
      const menu = root.querySelector('[data-more-menu]');
      if (!toggle || !menu) return;
      const close = () => {
        menu.hidden = true;
        toggle.setAttribute('aria-expanded', 'false');
      };
      const place = () => {
        const anchor = toggle.getBoundingClientRect();
        const rect = menu.getBoundingClientRect();
        menu.style.left = Math.max(8,Math.min(innerWidth - rect.width - 8,anchor.right - rect.width)) + 'px';
        menu.style.top = Math.max(8,Math.min(innerHeight - rect.height - 8,anchor.bottom + 7)) + 'px';
      };
      // A fixed menu must live outside the horizontally scrollable navigation,
      // otherwise the header clips it even though its links remain focusable.
      document.body.append(menu);
      toggle.addEventListener('click', event => {
        event.preventDefault();
        if (!menu.hidden) { close(); return; }
        menu.hidden = false;
        toggle.setAttribute('aria-expanded', 'true');
        place();
      });
      menu.querySelectorAll('a').forEach(link => link.addEventListener('click', close));
      document.addEventListener('click', event => {
        if (!event.target.closest('.refresh-more') && !menu.contains(event.target)) close();
      });
      document.addEventListener('keydown', event => {
        if (event.key === 'Escape' && !menu.hidden) { close(); toggle.focus(); }
      });
      document.addEventListener('scroll', () => { if (!menu.hidden) place(); }, true);
      window.addEventListener('resize', () => { if (!menu.hidden) place(); });
    },
    guard() {
      let latest = 0;
      return id => {
        latest = Math.max(latest, id);
        return id === latest;
      };
    },
    bindCardKeys(selector) {
      document.querySelectorAll(selector).forEach(card => {
        card.addEventListener('keydown', event => {
          if (event.key === 'Enter' || event.key === ' ') {
            event.preventDefault();
            card.click();
          }
        });
      });
    }
  };
})();
