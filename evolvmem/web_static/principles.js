/* Current product architecture; history capture and manual experience stay separate. */
(() => {
  'use strict';
  window.EvolvMemPrinciples = {
    render(policy) {
      return `<header class="page-head"><div><p class="eyebrow">EVOLVMEM / 工作原理</p><h1>每个人的知识，分成历史与经验</h1><p class="muted">历史保存原文与项目进展；经验由你选定，按三类持续维护。</p></div></header>
<div class="diagram-flow" role="img" aria-label="飞书登录确定当前账号，从用户知识库选择查看对象，检查有向权限后读取该用户的历史或经验">
  <div class="diagram-node"><strong>1 · 确定登录账号</strong><small>固定 8 小时登录，服务重启保留</small></div><span class="diagram-arrow" aria-hidden="true">→</span>
  <div class="diagram-node"><strong>2 · 用户知识库</strong><small>统一选择查看对象；登录账号不变</small></div><span class="diagram-arrow" aria-hidden="true">→</span>
  <div class="diagram-node"><strong>3 · 检查访问权限</strong><small>普通浏览逐方向授权；管理员查看为只读</small></div><span class="diagram-arrow" aria-hidden="true">→</span>
  <div class="diagram-node"><strong>4 · 历史 / 经验</strong><small>列表、详情和图片均检查权限；仅本人可管理</small></div>
</div>
<div class="principle-grid">
  <section class="panel"><h2>历史</h2><ol><li><b>项目摘要：</b>项目用途、环境与必要信息。</li><li><b>简短历史：</b>按时间查看做过的事项。</li><li><b>事项详情：</b>用户的问题、AI 的回答、解决方法和结果。</li></ol><p class="muted">未归属会话先按块保存。缺失原文会明确显示；图片只有能读取时才展示。删除进入回收站，原始资料保留。</p></section>
  <section class="panel"><h2>经验</h2><ol><li><b>前端经验：</b>界面、交互与页面开发中的做法。</li><li><b>业务经验：</b>业务规则、流程与计算中的做法。</li><li><b>系统环境经验：</b>部署、运行环境与服务配置中的做法。</li></ol><p>每条填写<b>问题、解决办法、可复用经验</b>。你可以在网页新增，也可以在会话中明确要求助手记住一条，再到网页查看和编辑。</p><p class="muted">会话中的明确指令由助手判断后逐条写入。旧提取稿保留，自动提取保持停止。</p>
    <div class="diagram-flow" role="img" aria-label="通过网页或当前会话的明确记忆指令，选择前端经验、业务经验或系统环境经验，保存问题、解决办法和可复用经验。本人可管理，其他用户按授权阅读">
      <div class="diagram-node"><strong>选择分类</strong><small>前端 / 业务 / 系统环境</small></div><span class="diagram-arrow" aria-hidden="true">→</span>
      <div class="diagram-node"><strong>整理一条经验</strong><small>网页填写或在会话中明确要求记住</small></div><span class="diagram-arrow" aria-hidden="true">→</span>
      <div class="diagram-node"><strong>保存和维护</strong><small>本人管理；他人按授权只读</small></div>
    </div>
  </section>
  <section class="panel"><h2>用户与交叉访问</h2><p>登录从登录时起固定 8 小时；主动退出或停用账号会立即撤销。重新启用后需再次登录。</p><p>A → B 和 B → A 是两个设置；历史与经验也分别授权。默认只可读取自己的知识。</p><p class="muted">普通浏览包括管理员也遵循授权。“用户知识库”是选择查看对象的统一入口。自己的资料可正常管理；查看其他用户时为只读，不能替他人新增、编辑或删除经验。停用用户后，后续读取会被拒绝。</p></section>
  <section class="panel"><h2>当前运行范围</h2><p>旧项目库与旧资料整理入口${policy.legacy_web_enabled === false ? '已关闭' : '状态以当前策略为准'}；AI 自动整理${policy.ai_processing_enabled === false ? '已暂停' : '状态以当前策略为准'}。</p><p class="muted">经验采用手动维护，自动提取保持停止。原文上传、项目归档与任务续接保留；这与经验提取是两条独立流程。原数据库和旧提取稿未删除。历史与经验页显示当前查看对象；用户管理和工作原理属于全局页面，不显示个人库归属。新建网页账号不代表已经创建 CLI 凭证或接入历史数据。</p></section>
</div>`;
    }
  };
})();
