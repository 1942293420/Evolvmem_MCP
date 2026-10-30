(() => {
  'use strict';
  function initPreview(root){
    const selected=new URLSearchParams(location.search).get('nav-design')==='cards'?'cards':'contrast';
    const header=root.querySelector('.refresh-topnav'),nav=header?.querySelector('.signal-nav');
    if(!nav)return false;
    const icons={
      home:'<rect x="3" y="3" width="7" height="7" rx="2"/><rect x="14" y="3" width="7" height="7" rx="2"/><rect x="3" y="14" width="7" height="7" rx="2"/><rect x="14" y="14" width="7" height="7" rx="2"/>',
      capabilities:'<path d="m12 3 9 5-9 5-9-5 9-5Z"/><path d="m3 12 9 5 9-5M3 16l9 5 9-5"/>',
      archives:'<path d="M3 7a2 2 0 0 1 2-2h5l2 2h7a2 2 0 0 1 2 2v10H3V7Z"/><path d="M8 12h8M8 16h5"/>',
      progress:'<circle cx="6" cy="5" r="2"/><circle cx="6" cy="19" r="2"/><circle cx="18" cy="12" r="2"/><path d="M6 7v10M8 5h4a6 6 0 0 1 6 5"/>',
      organization:'<path d="m12 3 2 6 7 3-7 3-2 6-2-6-7-3 7-3 2-6Z"/>',
      cleaning:'<path d="m15 3-5 8M6 11h8l4 9H2l4-9Z"/><path d="m7 15-1 5M11 15l1 5M18 6h4M20 4v4"/>',
      intake:'<path d="M3 12h5l2 4h4l2-4h5M5 4h14l2 8v8H3v-8l2-8Z"/>',
      skill:'<path d="M4 6h16M4 12h16M4 18h16"/><circle cx="8" cy="6" r="2"/><circle cx="16" cy="12" r="2"/><circle cx="10" cy="18" r="2"/>',
      projects:'<path d="M12 6c-3-2-6-2-9-1v15c3-1 6-1 9 1 3-2 6-2 9-1V5c-3-1-6-1-9 1Zm0 0v15"/>',
      qa:'<path d="M21 11a8 8 0 0 1-8 8H8l-5 3v-7a8 8 0 1 1 18-4Z"/><path d="M9 8a3 3 0 0 1 6 0c0 2-3 2-3 4M12 15h.01"/>',
      principles:'<rect x="8" y="2" width="8" height="6" rx="1"/><rect x="2" y="16" width="7" height="6" rx="1"/><rect x="15" y="16" width="7" height="6" rx="1"/><path d="M12 8v4H5v4m7-4h7v4"/>'
    };
    const links=new Map([...nav.querySelectorAll('a[data-page-link]')].map(a=>[a.dataset.knowledgeView||a.dataset.pageLink,a]));
    const groups=[['日常工作','work',['home','capabilities','archives','progress']],['整理与维护','manage',['organization','cleaning','intake','skill']],['历史与说明','history',['projects','qa','principles']]];
    nav.replaceChildren();nav.setAttribute('aria-label','按用途分组的主导航');
    for(const [title,tone,keys] of groups){
      const group=document.createElement('section');group.className='nav-preview-group';group.dataset.tone=tone;group.setAttribute('aria-label',title);group.title=title;
      const heading=document.createElement('h2');heading.textContent=title;group.append(heading);
      const list=document.createElement('div');list.className='nav-preview-links';group.append(list);
      for(const key of keys){const a=links.get(key);if(!a)continue;
        const label=key==='projects'?'旧项目知识':key==='qa'?'旧经验知识':a.textContent.trim().replace(/^[◫▦▥↳◎]\s*/,'');
        a.className='nav-preview-link';a.innerHTML=`<span class="nav-preview-icon" aria-hidden="true"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.65" stroke-linecap="round" stroke-linejoin="round">${icons[key]}</svg></span><span class="nav-preview-label"></span><span class="nav-preview-arrow" aria-hidden="true">›</span>`;
        a.querySelector('.nav-preview-label').textContent=label;list.append(a);
      }nav.append(group);
    }
    const brand=header.querySelector('.signal-brand');if(brand){brand.href='#home';brand.dataset.pageLink='home';}
    document.body.dataset.navDesign=selected;
    // Sticky height varies when groups wrap, fonts load or the account appears.
    // Keep in-page anchors and the capability sidebar below the frozen header.
    const measureHeader=()=>{
      const height=Math.ceil(header.getBoundingClientRect().height);
      document.documentElement.style.setProperty('--nav-header-height',height+'px');
      document.documentElement.style.scrollPaddingTop=(height+16)+'px';
    };
    new ResizeObserver(measureHeader).observe(header);
    measureHeader();return true;
  }

  window.EvolvRefresh = {
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
