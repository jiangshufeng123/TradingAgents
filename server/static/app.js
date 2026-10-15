/* TradingAgents 投研台: one page, hash routes, the /api of server/app.py. */
(() => {
const { createApp, reactive, ref, computed, watch, onMounted, onUnmounted, nextTick } = Vue;

// --- API ---------------------------------------------------------------------------
const store = reactive({ me: null, ready: false, route: parseRoute(), toasts: [], balance: null, active: { analysis: 0, screen: 0 } });

// Running and queued jobs per kind, for the badges on 分析 and 选股 (one poll for every page).
async function refreshActive() {
  if (!store.me) return;
  try {
    const list = await api("/jobs?limit=50");
    const live = list.filter(j => ["queued", "running", "cancelling"].includes(j.status));
    store.active = { analysis: live.filter(j => j.kind === "analysis").length, screen: live.filter(j => j.kind === "screen").length };
  } catch (e) { /* the next poll tries again */ }
}

class ApiError extends Error {
  constructor(status, message) { super(message); this.status = status; }
}

async function api(path, { method = "GET", body, raw = false } = {}) {
  const opts = { method, credentials: "same-origin", headers: { "X-TA-Client": "web" } };
  if (body !== undefined) { opts.headers["Content-Type"] = "application/json"; opts.body = JSON.stringify(body); }
  const res = await fetch("/api" + path, opts);
  if (res.status === 401 && path !== "/auth/login") { store.me = null; throw new ApiError(401, "请先登录"); }
  if (raw) return res;
  const text = await res.text();
  let data = null;
  try { data = text ? JSON.parse(text) : null; } catch { data = text; }
  if (!res.ok) {
    let msg = data && data.detail;
    if (Array.isArray(msg)) msg = msg.map(d => d.msg).join("；");
    throw new ApiError(res.status, msg || `请求失败（${res.status}）`);
  }
  return data;
}

function toast(text, kind = "") {
  const t = { id: Math.random(), text, kind };
  store.toasts.push(t);
  setTimeout(() => { store.toasts.splice(store.toasts.indexOf(t), 1); }, kind === "err" ? 6000 : 3200);
}
const fail = e => { if (e.status !== 401) toast(e.message, "err"); };

// --- routing -------------------------------------------------------------------------
function parseRoute() {
  const parts = (location.hash.replace(/^#\/?/, "") || "overview").split("/");
  return { name: parts[0], arg: parts[1] || null };
}
window.addEventListener("hashchange", () => { store.route = parseRoute(); window.scrollTo(0, 0); });
const go = path => { location.hash = "#/" + path; };

// --- security names: asked for in batches as tickers appear, kept for the session ----------
const names = reactive({});
const nameAsked = new Set();
let nameQueue = [], nameTimer = null;
function nameOf(t) {
  if (!t) return "";
  const k = String(t).toUpperCase();
  if (!(k in names) && !nameAsked.has(k)) {
    nameAsked.add(k); nameQueue.push(k);
    if (!nameTimer) nameTimer = setTimeout(flushNames, 120);
  }
  return names[k] || "";
}
async function flushNames() {
  nameTimer = null;
  if (!store.me) { nameTimer = setTimeout(flushNames, 500); return; }   // wait for sign-in, keep the queue
  const batch = nameQueue.splice(0, 60);
  if (!batch.length) return;
  try {
    const got = await api("/names?t=" + encodeURIComponent(batch.join(",")));
    for (const k of batch) names[k] = (got && got[k]) || "";
  } catch (e) {
    nameQueue.push(...batch);                                         // try again shortly
    nameTimer = setTimeout(flushNames, 5000);
    return;
  }
  if (nameQueue.length) nameTimer = setTimeout(flushNames, 50);
}
const withName = t => { const n = nameOf(t); return n ? `${t} ${n}` : t; };

// Code with its name after it, e.g. 600519.SS 贵州茅台
const Tk = {
  props: { t: String, big: Boolean },
  template: `<span class="tkn"><span class="num" :class="big && 'tk'">{{ t }}</span><span v-if="name" class="nm">{{ name }}</span></span>`,
  computed: { name() { return nameOf(this.t); } },
};

// --- formatting ----------------------------------------------------------------------
const RATING = {
  buy: ["买", "入", "buy"], overweight: ["增", "持", "over"], hold: ["持", "有", "hold"],
  underweight: ["减", "持", "under"], sell: ["卖", "出", "sell"],
};
const ratingKey = r => (r || "").toLowerCase().trim();
const ratingName = r => { const x = RATING[ratingKey(r)]; return x ? x[0] + x[1] : (r ? "待复核" : "—"); };
const ACTION = {
  "no trade": "不动", "keep what you hold": "持有不动", "open/add up to the full position": "建到满仓位",
  "open/add up to half the full position": "建到半仓位", "cut the position by half": "减半",
  "exit / do not open": "清仓或不开仓", review: "待复核",
};
function actionName(a) {
  if (!a) return "—";
  const m = /^(BUY|SELL)\s+([\d,.]+)/i.exec(a);
  if (m) return (m[1].toUpperCase() === "BUY" ? "买入 " : "卖出 ") + m[2] + " 股";
  return ACTION[a.trim().toLowerCase()] || a;
}
const pct = (v, d = 1) => v == null || isNaN(v) ? "—" : (v * 100).toFixed(d) + "%";
const signed = (v, d = 2) => v == null || isNaN(v) ? "—" : (v > 0 ? "+" : "") + (v * 100).toFixed(d) + "%";
const money = (v, d = 2) => v == null || isNaN(v) ? "—" : Number(v).toLocaleString("zh-CN", { minimumFractionDigits: d, maximumFractionDigits: d });
const numf = (v, d = 2) => v == null || v === "" || isNaN(v) ? "—" : Number(v).toLocaleString("zh-CN", { maximumFractionDigits: d });
const tone = v => v > 0 ? "up" : v < 0 ? "down" : "";
function when(iso) {
  if (!iso) return "—";
  const d = new Date(iso), now = new Date(), diff = (now - d) / 1000;
  const hm0 = d.toTimeString().slice(0, 5);
  if (diff < 0) {                       // a time still to come (the next scheduled run)
    if (d.toDateString() === now.toDateString()) return "今天 " + hm0;
    const t = new Date(now); t.setDate(now.getDate() + 1);
    if (d.toDateString() === t.toDateString()) return "明天 " + hm0;
    return `${d.getMonth() + 1}月${d.getDate()}日 ${hm0}`;
  }
  if (diff < 60) return "刚刚";
  if (diff < 3600) return Math.floor(diff / 60) + " 分钟前";
  const sameDay = d.toDateString() === now.toDateString();
  const hm = d.toTimeString().slice(0, 5);
  if (sameDay) return "今天 " + hm;
  const y = new Date(now); y.setDate(now.getDate() - 1);
  if (d.toDateString() === y.toDateString()) return "昨天 " + hm;
  return `${d.getMonth() + 1}月${d.getDate()}日 ${hm}`;
}
function duration(a, b) {
  if (!a) return "";
  const s = Math.max(0, ((b ? new Date(b) : new Date()) - new Date(a)) / 1000);
  if (s < 60) return Math.round(s) + " 秒";
  if (s < 3600) return Math.round(s / 60) + " 分钟";
  return (s / 3600).toFixed(1) + " 小时";
}
const STATUS = { queued: "排队中", running: "运行中", cancelling: "正在停止", done: "已完成", failed: "失败", cancelled: "已取消" };
const KIND = { analysis: "分析", screen: "选股" };
const jobTitle = j => j.kind === "screen"
  ? `选股：${j.params.request || "按条件筛选"}`
  : `分析 ${(j.params.tickers || []).map(withName).join("、")}`;

const FIELDS = {
  price: ["最新价", "元"], chg_pct: ["今日涨跌", "%"], turnover_pct: ["换手率", "%"], volume_ratio: ["量比", ""],
  amount: ["成交额", "亿元"], pe: ["市盈率", "倍"], pb: ["市净率", "倍"], mcap: ["总市值", "亿元"],
  float_mcap: ["流通市值", "亿元"], chg_60d: ["60日涨跌", "%"], chg_ytd: ["年初至今涨跌", "%"],
  revenue_yoy: ["营收同比", "%"], profit_yoy: ["净利同比", "%"], roe: ["ROE（报告期）", "%"],
  roe_annualized: ["ROE（年化）", "%"], gross_margin: ["毛利率", "%"], eps: ["每股收益", "元"],
  ocf_per_share: ["每股经营现金流", "元"],
};
const OPS = [[">", "大于"], [">=", "不低于"], ["<", "小于"], ["<=", "不高于"], ["between", "介于"]];

// --- icons (18px, 1.6 stroke) ----------------------------------------------------------
const ICON = {
  overview: '<path d="M4 13h6V4H4zM14 20h6v-9h-6zM4 20h6v-4H4zM14 7h6V4h-6z"/>',
  screen: '<path d="M4 5h16l-6 7.5V19l-4 1v-7.5z"/>',
  jobs: '<path d="M5 4h14v16H5z"/><path d="M9 9l5 3-5 3z"/>',
  watch: '<path d="M12 4l2.4 5 5.4.6-4 3.7 1.1 5.3L12 16l-4.9 2.6 1.1-5.3-4-3.7 5.4-.6z"/>',
  book: '<path d="M4 7h16v12H4z"/><path d="M8 7V5h8v2M4 12h16"/>',
  sources: '<ellipse cx="12" cy="6" rx="7" ry="2.5"/><path d="M5 6v6c0 1.4 3.1 2.5 7 2.5s7-1.1 7-2.5V6M5 12v6c0 1.4 3.1 2.5 7 2.5s7-1.1 7-2.5v-6"/>',
  settings: '<circle cx="12" cy="12" r="3"/><path d="M12 3v2.5M12 18.5V21M3 12h2.5M18.5 12H21M5.6 5.6l1.8 1.8M16.6 16.6l1.8 1.8M5.6 18.4l1.8-1.8M16.6 7.4l1.8-1.8"/>',
};
const Icon = {
  props: ["name"],
  template: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" v-html="paths"></svg>`,
  computed: { paths() { return ICON[this.name] || ""; } },
};

// --- shared pieces ---------------------------------------------------------------------
const Seal = {
  props: { rating: String, small: Boolean },
  template: `<span class="seal" :class="[cls, small && 'sm']" :title="rating || '无评级'" role="img" :aria-label="'评级：' + label">
    <template v-if="chars"><b>{{ chars[0] }}</b><b>{{ chars[1] }}</b></template><template v-else>{{ rating ? '复核' : '—' }}</template></span>`,
  computed: {
    chars() { const x = RATING[ratingKey(this.rating)]; return x ? [x[0], x[1]] : null; },
    cls() { const x = RATING[ratingKey(this.rating)]; return x ? x[2] : "review"; },
    label() { return ratingName(this.rating); },
  },
};

const StatusTag = {
  props: ["status"],
  template: `<span class="tag" :class="status">{{ text }}</span>`,
  computed: { text() { return STATUS[this.status] || this.status; } },
};

const Empty = {
  props: ["text"],
  template: `<div class="board-empty"><p>{{ text }}</p><slot/></div>`,
};

// --- equity chart: account vs benchmark, cumulative return, one axis -----------------
const EquityChart = {
  props: { rows: Array, benchmark: String },
  setup(props) {
    const hover = ref(null);
    const showTable = ref(false);
    // Drawn at the container's own width, so axis text stays 11px on a phone.
    const box = ref(null), width = ref(720);
    let ro;
    onMounted(() => {
      ro = new ResizeObserver(([e]) => { width.value = Math.max(300, Math.round(e.contentRect.width)); });
      if (box.value) ro.observe(box.value);
    });
    watch(box, (el, old) => { if (!ro) return; if (old) ro.unobserve(old); if (el) ro.observe(el); });
    onUnmounted(() => ro && ro.disconnect());
    const H = 240, L = 44, R = 58, T = 12, B = 26;
    const pts = computed(() => (props.rows || []).filter(r => r.return != null));
    const scale = computed(() => {
      const ys = pts.value.flatMap(r => [r.return, r.benchmark_return]).filter(v => v != null);
      let lo = Math.min(0, ...ys), hi = Math.max(0, ...ys);
      if (hi - lo < 0.02) { hi += 0.01; lo -= 0.01; }
      const pad = (hi - lo) * 0.08; lo -= pad; hi += pad;
      const n = Math.max(pts.value.length - 1, 1);
      return {
        x: i => L + (width.value - L - R) * i / n,
        y: v => T + (H - T - B) * (hi - v) / (hi - lo),
        ticks: niceTicks(lo, hi, 4),
      };
    });
    function niceTicks(lo, hi, n) {
      const step0 = (hi - lo) / n, mag = Math.pow(10, Math.floor(Math.log10(step0)));
      const step = [1, 2, 2.5, 5, 10].map(m => m * mag).find(s => s >= step0);
      const out = [];
      for (let v = Math.ceil(lo / step) * step; v <= hi + 1e-9; v += step) out.push(+v.toFixed(6));
      return out;
    }
    const path = key => {
      let d = "", s = scale.value;
      pts.value.forEach((r, i) => {
        if (r[key] == null) return;
        d += (d ? "L" : "M") + s.x(i).toFixed(1) + "," + s.y(r[key]).toFixed(1);
      });
      return d;
    };
    const last = computed(() => pts.value[pts.value.length - 1]);
    const xLabels = computed(() => {
      const p = pts.value; if (!p.length) return [];
      const idx = p.length < 3 ? p.map((_, i) => i) : [0, Math.floor((p.length - 1) / 2), p.length - 1];
      return [...new Set(idx)].map(i => ({ i, text: p[i].date.slice(5) }));
    });
    function move(ev) {
      const svg = ev.currentTarget, rect = svg.getBoundingClientRect();
      const W = width.value, x = (ev.clientX - rect.left) * W / rect.width;
      const n = pts.value.length; if (!n) return;
      const i = Math.max(0, Math.min(n - 1, Math.round((x - L) / ((W - L - R) / Math.max(n - 1, 1)))));
      hover.value = { i, left: scale.value.x(i) * rect.width / W, row: pts.value[i] };
    }
    return { W: width, H, L, R, T, B, box, pts, scale, path, last, xLabels, hover, move, showTable, signed };
  },
  template: `<div>
    <div class="row" style="justify-content:space-between;margin-bottom:6px">
      <div class="legend"><span><i style="background:var(--series-1)"></i>模拟账户</span><span><i style="background:var(--series-2)"></i>基准 {{ benchmark }}</span></div>
      <button class="btn quiet small" @click="showTable = !showTable">{{ showTable ? '看图' : '看数据表' }}</button>
    </div>
    <div v-if="pts.length < 2 && !showTable" class="note">账户至少运行两个交易日后，这里会画出收益曲线。</div>
    <div v-else-if="!showTable" class="chart" ref="box">
      <svg :viewBox="'0 0 ' + W + ' ' + H" @mousemove="move" @mouseleave="hover = null" role="img" aria-label="模拟账户与基准的累计收益">
        <g v-for="t in scale.ticks" :key="t">
          <line :class="t === 0 ? 'zero' : 'gridline'" :x1="L" :x2="W - R" :y1="scale.y(t)" :y2="scale.y(t)"/>
          <text class="axis" :x="L - 8" :y="scale.y(t) + 4" text-anchor="end">{{ (t * 100).toFixed(Math.abs(t) < 0.1 ? 1 : 0) }}%</text>
        </g>
        <text v-for="x in xLabels" :key="x.i" class="axis" :x="scale.x(x.i)" :y="H - 6" text-anchor="middle">{{ x.text }}</text>
        <path :d="path('benchmark_return')" class="s2" fill="none" stroke-width="2" stroke-linejoin="round" stroke-linecap="round"/>
        <path :d="path('return')" class="s1" fill="none" stroke-width="2" stroke-linejoin="round" stroke-linecap="round"/>
        <template v-if="last">
          <circle v-if="last.benchmark_return != null" class="dot2" :cx="scale.x(pts.length - 1)" :cy="scale.y(last.benchmark_return)" r="4"/>
          <circle class="dot1" :cx="scale.x(pts.length - 1)" :cy="scale.y(last.return)" r="4"/>
          <text class="endlabel" :x="W - R + 8" :y="scale.y(last.return) + 4">{{ signed(last.return, 1) }}</text>
          <text v-if="last.benchmark_return != null" class="endlabel" :x="W - R + 8" :y="scale.y(last.benchmark_return) + (Math.abs(scale.y(last.benchmark_return) - scale.y(last.return)) < 14 ? 18 : 4)">{{ signed(last.benchmark_return, 1) }}</text>
        </template>
        <g v-if="hover">
          <line class="cross" :x1="scale.x(hover.i)" :x2="scale.x(hover.i)" :y1="T" :y2="H - B"/>
          <circle class="dot1" :cx="scale.x(hover.i)" :cy="scale.y(hover.row.return)" r="4.5"/>
          <circle v-if="hover.row.benchmark_return != null" class="dot2" :cx="scale.x(hover.i)" :cy="scale.y(hover.row.benchmark_return)" r="4.5"/>
        </g>
        <rect :x="L" :y="T" :width="W - L - R" :height="H - T - B" fill="transparent"/>
      </svg>
      <div v-if="hover" class="tip" :style="{ left: Math.min(hover.left + 12, W - 170) + 'px', top: '8px' }">
        <div class="num" style="margin-bottom:4px">{{ hover.row.date }}</div>
        <div class="k"><span><i style="background:var(--series-1)"></i>模拟账户</span><span class="num">{{ signed(hover.row.return) }}</span></div>
        <div class="k"><span><i style="background:var(--series-2)"></i>基准</span><span class="num">{{ signed(hover.row.benchmark_return) }}</span></div>
      </div>
    </div>
    <div v-else class="table-wrap"><table class="t"><thead><tr><th>日期</th><th class="r">账户累计</th><th class="r">基准累计</th><th class="r">总资产</th></tr></thead>
      <tbody><tr v-for="r in [...rows].reverse()" :key="r.date"><td class="num">{{ r.date }}</td>
        <td class="r num" :class="r.return > 0 ? 'up' : r.return < 0 ? 'down' : ''">{{ signed(r.return) }}</td>
        <td class="r num">{{ signed(r.benchmark_return) }}</td><td class="r num">{{ r.equity == null ? '—' : r.equity.toLocaleString('zh-CN') }}</td></tr></tbody></table></div>
  </div>`,
};

// --- login ---------------------------------------------------------------------------
const LoginView = {
  setup() {
    const username = ref(""), password = ref(""), busy = ref(false), error = ref("");
    async function submit() {
      busy.value = true; error.value = "";
      try {
        const r = await api("/auth/login", { method: "POST", body: { username: username.value, password: password.value } });
        store.me = r.user;
        if (!location.hash || location.hash === "#/") go("overview");
      } catch (e) { error.value = e.message; } finally { busy.value = false; }
    }
    return { username, password, busy, error, submit };
  },
  template: `<div class="login"><div class="login-box">
    <div class="brand"><div class="brand-mark">研</div><div class="brand-name">TradingAgents 投研台<span>多智能体 A 股 / 港股研究</span></div></div>
    <form class="panel panel-body" @submit.prevent="submit">
      <h1 style="font-size:20px">登录</h1>
      <label class="field"><span>用户名</span><input class="input" v-model.trim="username" autocomplete="username" required autofocus></label>
      <label class="field"><span>密码</span><input class="input" type="password" v-model="password" autocomplete="current-password" required></label>
      <div v-if="error" class="note err" role="alert">{{ error }}</div>
      <button class="btn primary" :disabled="busy">{{ busy ? '正在登录…' : '登录' }}</button>
    </form>
    <p class="muted small" style="margin-top:14px">账号由管理员在“设置”里创建。忘记密码请联系管理员重置。</p>
  </div></div>`,
};

// --- overview -------------------------------------------------------------------------
const OverviewView = {
  components: { Seal, StatusTag, Empty },
  setup() {
    const data = ref(null), balance = ref(null);
    let timer;
    async function load() {
      try {
        data.value = await api("/overview");
      } catch (e) { fail(e); }
    }
    async function loadBalance() { try { balance.value = await api("/balance"); store.balance = balance.value; } catch (e) { /* shown as unavailable */ } }
    onMounted(() => { load(); loadBalance(); timer = setInterval(load, 8000); });
    onUnmounted(() => clearInterval(timer));
    const spendShare = computed(() => {
      const d = data.value; if (!d || !d.monthly_quota) return null;
      return Math.min(1, d.month_spend / d.monthly_quota);
    });
    const cny = computed(() => {
      const b = balance.value; if (!b || !b.ok) return null;
      return b.balances.find(x => x.total) || b.balances[0] || null;
    });
    const activeByKind = computed(() => {
      const out = { analysis: 0, screen: 0 };
      for (const j of (data.value ? data.value.active_jobs : [])) out[j.kind] = (out[j.kind] || 0) + 1;
      return out;
    });
    const reportUrl = d => d.job_id != null && d.index != null ? `/api/jobs/${d.job_id}/report/${d.index}` : `/api/report?dir=${encodeURIComponent(d.report_dir)}`;
    return { data, balance, cny, spendShare, activeByKind, reportUrl, KIND, when, pct, numf, money, actionName, jobTitle, duration, go };
  },
  template: `<div class="page">
    <div class="page-head">
      <div><h1>今日结论</h1><p>每只股票最近一次分析给出的评级、目标仓位和止损价。</p></div>
      <div class="row"><a class="btn" href="#/screen">选股</a><a class="btn primary" href="#/jobs">发起分析</a></div>
    </div>
    <div class="split">
      <div>
        <div class="panel">
          <ul class="board" v-if="data && data.decisions.length">
            <li v-for="d in data.decisions" :key="d.ticker + (d.job_id || '')">
              <Seal :rating="d.rating"/>
              <div><Tk :t="d.ticker" big/><div class="meta">{{ d.trade_date || '—' }}<template v-if="d.vote_ratings && d.vote_ratings.includes(' ')">，投票 {{ d.vote_ratings.split(' ').length }} 次</template></div></div>
              <div class="kv"><span>操作</span><strong style="font-family:var(--font);font-size:15px">{{ actionName(d.action) }}</strong></div>
              <div class="kv"><span>目标仓位</span><strong>{{ d.target_weight == null ? '—' : pct(d.target_weight) }}</strong></div>
              <div class="kv hide-s"><span>止损</span><strong>{{ numf(d.stop) }}</strong></div>
              <div><a v-if="d.report_dir" :href="reportUrl(d)" target="_blank" rel="noopener">报告</a>
                   <a v-else-if="d.job_id" :href="'#/jobs/' + d.job_id">任务</a></div>
            </li>
          </ul>
          <Empty v-else-if="data" text="还没有分析结果。把股票加入自选股，然后发起一次分析。">
            <a class="btn primary" href="#/watchlist">管理自选股</a>
          </Empty>
          <div v-else class="board-empty muted">正在读取…</div>
        </div>

        <section class="section" v-if="data && data.recent_jobs.length">
          <h2>最近任务</h2>
          <div class="panel table-wrap"><table class="t"><tbody>
            <tr v-for="j in data.recent_jobs" :key="j.id" style="cursor:pointer" @click="go('jobs/' + j.id)">
              <td style="width:92px"><StatusTag :status="j.status"/></td>
              <td><a :href="'#/jobs/' + j.id" @click.stop>{{ jobTitle(j) }}</a>
                <div v-if="j.progress" class="muted small">第 {{ j.progress.index }}/{{ j.progress.total }} 只：{{ withName(j.progress.ticker) }}<template v-if="j.progress.step">，{{ j.progress.step }}</template></div></td>
              <td class="r muted small hide-s">{{ when(j.created_at) }}</td>
              <td class="r num small" style="width:90px">{{ j.cost == null ? '' : '¥' + money(j.cost) }}</td>
            </tr></tbody></table></div>
        </section>
      </div>

      <aside class="facts" v-if="data">
        <div class="fact">
          <span>DeepSeek 余额</span>
          <template v-if="cny"><div class="big">{{ cny.currency === 'CNY' ? '¥' : cny.currency + ' ' }}{{ money(cny.total) }}</div>
            <div class="sub">充值 {{ money(cny.topped_up) }}，赠送 {{ money(cny.granted) }}</div></template>
          <div v-else class="sub">{{ balance ? (balance.error || '读取不到') : '正在读取…' }}</div>
        </div>
        <div class="fact">
          <span>本月模型花费</span>
          <div class="big">¥{{ money(data.month_spend) }}</div>
          <template v-if="data.monthly_quota"><div class="meter" :class="spendShare > .8 && 'hot'"><i :style="{ width: (spendShare * 100) + '%' }"></i></div>
            <div class="sub">额度 ¥{{ money(data.monthly_quota) }}，已用 {{ pct(spendShare, 0) }}</div></template>
          <div v-else class="sub">未设额度</div>
        </div>
        <div class="fact">
          <span>正在进行</span>
          <div class="big">{{ data.active_jobs.length }}</div>
          <div class="sub" v-if="data.active_jobs.length">
            <template v-for="(n, k) in activeByKind" :key="k"><span v-if="n">{{ KIND[k] }} {{ n }} 个　</span></template></div>
          <div class="sub" v-else>没有排队或运行中的任务</div>
        </div>
        <div class="fact">
          <span>每日定时分析</span>
          <template v-if="data.schedule.enabled"><div class="big">{{ data.schedule.time_of_day }}</div>
            <div class="sub">下次 {{ when(data.schedule.next_run) }}，自选股 {{ data.watchlist_count }} 只</div></template>
          <div v-else class="sub">未开启。<a href="#/settings">去设置</a></div>
        </div>
      </aside>
    </div>
  </div>`,
};

// --- screening ------------------------------------------------------------------------
const ScreenView = {
  components: { StatusTag },
  setup() {
    const request = ref(""), job = ref(null), busy = ref(false), picked = ref(new Set()), spec = ref(null);
    const history = ref([]);
    let timer;
    async function loadHistory() {
      try { history.value = (await api("/jobs?limit=30")).filter(j => j.kind === "screen").slice(0, 8); } catch (e) { fail(e); }
    }
    async function open(id) {
      try {
        job.value = await api("/jobs/" + id);
        picked.value = new Set();
        spec.value = job.value.result && job.value.result.spec ? JSON.parse(JSON.stringify(job.value.result.spec)) : null;
        if (["queued", "running", "cancelling"].includes(job.value.status)) poll(id);
      } catch (e) { fail(e); }
    }
    function poll(id) {
      clearInterval(timer);
      timer = setInterval(async () => {
        try {
          const j = await api("/jobs/" + id);
          job.value = j;
          if (!["queued", "running", "cancelling"].includes(j.status)) {
            clearInterval(timer); loadHistory();
            spec.value = j.result && j.result.spec ? JSON.parse(JSON.stringify(j.result.spec)) : null;
            if (j.status === "failed") toast("选股没有完成：" + (j.error || ""), "err");
          }
        } catch (e) { clearInterval(timer); }
      }, 2000);
    }
    async function run(body) {
      busy.value = true;
      try { const j = await api("/screens", { method: "POST", body }); job.value = j; spec.value = null; picked.value = new Set(); poll(j.id); }
      catch (e) { fail(e); } finally { busy.value = false; }
    }
    const start = () => request.value.trim() && run({ request: request.value.trim() });
    const rerun = () => run({ request: (job.value && job.value.params.request) || request.value, spec: spec.value });
    function addCond() { spec.value.filters.push({ field: "pe", op: "<", value: 20 }); }
    function setOp(f, op) {
      if (op === "between" && !Array.isArray(f.value)) f.value = [f.value || 0, f.value || 0];
      if (op !== "between" && Array.isArray(f.value)) f.value = f.value[0];
      f.op = op;
    }
    function toggle(n) { const s = new Set(picked.value); s.has(n) ? s.delete(n) : s.add(n); picked.value = s; }
    const rows = computed(() => (job.value && job.value.result && job.value.result.rows) || []);
    const allPicked = computed(() => rows.value.length && rows.value.every((r, i) => !r.ticker || picked.value.has(i + 1)));
    function toggleAll() {
      picked.value = allPicked.value ? new Set() : new Set(rows.value.map((r, i) => r.ticker ? i + 1 : null).filter(Boolean));
    }
    async function addPicked() {
      try {
        const r = await api(`/screens/${job.value.id}/pick`, { method: "POST", body: { rows: [...picked.value].sort((a, b) => a - b) } });
        const parts = [];
        if (r.added.length) parts.push(`已加入自选股：${r.added.join("、")}`);
        if (r.already.length) parts.push(`已在自选股里：${r.already.join("、")}`);
        if (r.no_symbol.length) parts.push(`没有可分析的代码（北交所等）：${r.no_symbol.join("、")}`);
        toast(parts.join("；") || "没有加入新的股票");
        picked.value = new Set();
      } catch (e) { fail(e); }
    }
    onMounted(() => { loadHistory(); if (store.route.arg) open(store.route.arg); });
    onUnmounted(() => clearInterval(timer));
    const fieldList = Object.entries(FIELDS);
    return { request, job, busy, picked, spec, history, rows, allPicked, fieldList, FIELDS, OPS,
             start, rerun, addCond, setOp, toggle, toggleAll, addPicked, open, numf, when, jobTitle };
  },
  template: `<div class="page">
    <div class="page-head"><div><h1>选股</h1><p>用一句话描述想要的股票，系统把它翻译成条件并在全市场 A 股里筛选。结果不会自动加入自选股。</p></div></div>

    <form class="panel panel-body" @submit.prevent="start">
      <label class="field"><span>想找什么样的股票</span>
        <textarea class="input" rows="2" v-model="request" placeholder="例如：市值 100 亿以上、市盈率低于 20、ROE 超过 15% 的消费股，按 ROE 从高到低"></textarea></label>
      <div class="row" style="margin-top:12px;justify-content:space-between">
        <span class="muted small">只调用一次快速模型，花费很少。</span>
        <button class="btn primary" :disabled="busy || !request.trim()">开始选股</button>
      </div>
    </form>

    <section class="section" v-if="job">
      <div class="row" style="justify-content:space-between;margin-bottom:12px">
        <h2>{{ job.params.request || '按条件筛选' }}</h2><StatusTag :status="job.status"/>
      </div>
      <div v-if="['queued','running','cancelling'].includes(job.status)" class="note">正在下载全市场行情并理解你的描述，通常需要 10–40 秒。</div>
      <div v-if="job.status === 'failed'" class="note err">{{ job.error }}</div>

      <template v-if="job.status === 'done' && spec">
        <div class="panel panel-body">
          <div class="row" style="justify-content:space-between;margin-bottom:10px">
            <h3>筛选条件</h3><span class="muted small">{{ job.result.source }}；业绩期 {{ job.result.period }}</span>
          </div>
          <p v-if="spec.explanation" class="muted small" style="margin-bottom:10px">{{ spec.explanation }}</p>
          <div class="conds">
            <div class="cond" v-for="(f, i) in spec.filters" :key="i">
              <select class="input" v-model="f.field" :aria-label="'条件 ' + (i + 1) + ' 指标'">
                <option v-for="[k, v] in fieldList" :key="k" :value="k">{{ v[0] }}{{ v[1] ? '（' + v[1] + '）' : '' }}</option></select>
              <select class="input" :value="f.op" @change="setOp(f, $event.target.value)" aria-label="比较方式">
                <option v-for="[k, v] in OPS" :key="k" :value="k">{{ v }}</option></select>
              <template v-if="f.op === 'between'">
                <input class="input num" type="number" step="any" v-model.number="f.value[0]" aria-label="下限">
                <input class="input num hi" type="number" step="any" v-model.number="f.value[1]" aria-label="上限">
              </template>
              <template v-else><input class="input num" type="number" step="any" v-model.number="f.value" aria-label="数值"><span class="hi"></span></template>
              <button class="btn quiet small" type="button" @click="spec.filters.splice(i, 1)" :aria-label="'删除条件 ' + (i + 1)">✕</button>
            </div>
          </div>
          <div class="row" style="margin-top:12px">
            <button class="btn small" type="button" @click="addCond">添加条件</button>
            <label class="check small"><input type="checkbox" v-model="spec.exclude_st">排除 ST</label>
            <label class="check small">最多 <input class="input num" style="width:72px;min-height:30px" type="number" min="1" max="200" v-model.number="spec.limit"> 只</label>
            <span v-if="spec.industries.length" class="muted small">行业：{{ spec.industries.join('、') }}</span>
            <span class="grow"></span>
            <button class="btn primary small" type="button" @click="rerun" :disabled="busy">按修改后的条件重新筛选</button>
          </div>
          <div v-if="job.result.unavailable.length" class="note warn" style="margin-top:12px">
            这些指标今天的数据源没有提供，相关条件不会命中任何股票：{{ job.result.unavailable.map(f => FIELDS[f] ? FIELDS[f][0] : f).join('、') }}</div>
        </div>

        <div class="panel table-wrap" style="margin-top:16px">
          <table class="t wide" v-if="rows.length">
            <thead><tr>
              <th style="width:36px"><input type="checkbox" class="check" :checked="allPicked" @change="toggleAll" aria-label="全选"></th>
              <th>#</th><th>代码</th><th>名称</th><th>行业</th><th class="r">最新价</th><th class="r">涨跌</th><th class="r">市盈率</th>
              <th class="r">市净率</th><th class="r">市值（亿）</th><th class="r">ROE 年化</th><th class="r">营收同比</th><th class="r">净利同比</th></tr></thead>
            <tbody><tr v-for="(r, i) in rows" :key="i" :class="picked.has(i + 1) && 'picked'" @click="r.ticker && toggle(i + 1)" style="cursor:pointer">
              <td @click.stop><input type="checkbox" :disabled="!r.ticker" :checked="picked.has(i + 1)" @change="toggle(i + 1)" :aria-label="'选择 ' + r.name"></td>
              <td class="muted num">{{ i + 1 }}</td><td class="num">{{ r.ticker || '—' }}</td><td>{{ r.name }}</td><td class="muted">{{ r.industry || '—' }}</td>
              <td class="r num">{{ numf(r.price) }}</td><td class="r num" :class="r.chg_pct > 0 ? 'up' : r.chg_pct < 0 ? 'down' : ''">{{ r.chg_pct == null ? '—' : (r.chg_pct > 0 ? '+' : '') + numf(r.chg_pct) + '%' }}</td>
              <td class="r num">{{ numf(r.pe, 1) }}</td><td class="r num">{{ numf(r.pb, 2) }}</td><td class="r num">{{ numf(r.mcap, 0) }}</td>
              <td class="r num">{{ numf(r.roe_annualized, 1) }}</td><td class="r num">{{ numf(r.revenue_yoy, 1) }}</td><td class="r num">{{ numf(r.profit_yoy, 1) }}</td>
            </tr></tbody></table>
          <div v-else class="board-empty">没有股票同时满足这些条件。放宽一两个条件再试。</div>
        </div>
        <div class="actionbar" v-if="picked.size">
          <span>已选 {{ picked.size }} 只</span>
          <button class="btn" @click="addPicked">加入自选股</button>
        </div>
      </template>
    </section>

    <section class="section" v-if="history.length">
      <h2>最近的选股</h2>
      <div class="panel table-wrap"><table class="t"><tbody>
        <tr v-for="h in history" :key="h.id" style="cursor:pointer" @click="open(h.id)">
          <td style="width:92px"><StatusTag :status="h.status"/></td><td>{{ h.params.request || '按条件筛选' }}</td><td class="r muted small when">{{ when(h.created_at) }}</td></tr>
      </tbody></table></div>
    </section>
  </div>`,
};

// --- analysis jobs --------------------------------------------------------------------
const JobsView = {
  components: { StatusTag },
  setup() {
    const watch_ = ref([]), chosen = ref(new Set()), extra = ref(""), runs = ref(null), date = ref("");
    const estimate = ref(null), jobs = ref([]), busy = ref(false);
    let timer;
    async function load() {
      try {
        jobs.value = (await api("/jobs?limit=40")).filter(j => j.kind === "analysis");
      } catch (e) { fail(e); }
    }
    onMounted(async () => {
      try { watch_.value = await api("/watchlist"); chosen.value = new Set(watch_.value.map(w => w.ticker)); } catch (e) { fail(e); }
      load(); timer = setInterval(load, 5000);
    });
    onUnmounted(() => clearInterval(timer));
    const tickers = computed(() => [...chosen.value, ...extra.value.split(/[,，\s;；]+/).map(s => s.trim().toUpperCase()).filter(Boolean)]);
    const body = () => ({ tickers: [...new Set(tickers.value)], runs: runs.value || null, date: date.value || null });
    let estTimer;
    watch([tickers, runs, date], () => {
      clearTimeout(estTimer);
      estimate.value = null;
      if (!tickers.value.length) return;
      estTimer = setTimeout(async () => { try { estimate.value = await api("/analyses/estimate", { method: "POST", body: body() }); } catch (e) { estimate.value = { error: e.message }; } }, 400);
    }, { immediate: false });
    function toggle(t) { const s = new Set(chosen.value); s.has(t) ? s.delete(t) : s.add(t); chosen.value = s; }
    async function start() {
      busy.value = true;
      try { const j = await api("/analyses", { method: "POST", body: body() }); toast("已排队：" + jobTitle(j)); go("jobs/" + j.id); }
      catch (e) { fail(e); } finally { busy.value = false; }
    }
    return { watch_, chosen, extra, runs, date, estimate, jobs, busy, tickers, toggle, start, when, money, duration, jobTitle };
  },
  template: `<div class="page">
    <div class="page-head"><div><h1>分析</h1><p>多位分析师、多空辩论、风控和组合经理依次给出结论。每只股票每次运行通常需要 5–15 分钟。</p></div></div>
    <form class="panel panel-body" @submit.prevent="start">
      <div class="field"><span>自选股（点击取消或选中）</span>
        <div class="chips" v-if="watch_.length"><button type="button" class="chip" :class="chosen.has(w.ticker) && 'on'" v-for="w in watch_" :key="w.ticker" @click="toggle(w.ticker)" :aria-pressed="chosen.has(w.ticker)">{{ w.ticker }}<span v-if="nameOf(w.ticker)" class="nm">{{ nameOf(w.ticker) }}</span></button></div>
        <span v-else class="muted small">自选股是空的，可以在下面直接输入代码。</span></div>
      <div class="form-grid" style="margin-top:14px">
        <label class="field"><span>其他代码（逗号分隔）</span><input class="input num" v-model="extra" placeholder="例如 600519.SS, 0700.HK, NVDA"></label>
        <label class="field"><span>每只运行几次（投票）</span><input class="input num" type="number" min="1" max="5" v-model.number="runs" placeholder="默认"></label>
        <label class="field"><span>分析日期</span><input class="input num" type="date" v-model="date"></label>
      </div>
      <div class="row" style="margin-top:16px;justify-content:space-between">
        <span class="small" :class="estimate && estimate.within_quota === false ? 'down' : 'muted'">
          <template v-if="!tickers.length">选择至少一只股票。</template>
          <template v-else-if="!estimate">{{ tickers.length }} 只股票，正在估算费用…</template>
          <template v-else-if="estimate.error">{{ estimate.error }}</template>
          <template v-else>{{ estimate.tickers.length }} 只 × {{ estimate.runs }} 次，
            <template v-if="estimate.estimate != null">预计约 ¥{{ money(estimate.estimate) }}</template><template v-else>还没有历史花费可供估算</template><template v-if="estimate.quota_remaining != null">；本月额度剩余 ¥{{ money(estimate.quota_remaining) }}</template><template v-if="estimate.within_quota === false">，超出额度</template></template>
        </span>
        <button class="btn primary" :disabled="busy || !tickers.length || (estimate && estimate.within_quota === false)">开始分析</button>
      </div>
    </form>

    <section class="section">
      <h2>分析任务</h2>
      <div class="panel table-wrap">
        <table class="t" v-if="jobs.length"><thead><tr><th>状态</th><th>股票</th><th class="hide-s">提交</th><th class="r hide-s">用时</th><th class="r">花费</th></tr></thead>
          <tbody><tr v-for="j in jobs" :key="j.id" style="cursor:pointer" @click="location.hash = '#/jobs/' + j.id">
            <td style="width:96px"><StatusTag :status="j.status"/></td>
            <td><a :href="'#/jobs/' + j.id" @click.stop>{{ j.params.tickers.map(withName).join('、') }}</a>
              <span v-if="j.params.scheduled" class="tag plain" style="margin-left:6px">定时</span>
              <div v-if="j.progress" class="muted small">第 {{ j.progress.index }}/{{ j.progress.total }} 只：{{ withName(j.progress.ticker) }}<template v-if="j.progress.step">，{{ j.progress.step }}</template></div></td>
            <td class="muted small hide-s">{{ when(j.created_at) }}</td>
            <td class="r small muted hide-s">{{ duration(j.started_at, j.finished_at) }}</td>
            <td class="r num small">{{ j.cost == null ? '' : '¥' + money(j.cost) }}</td></tr></tbody></table>
        <div v-else class="board-empty">还没有分析任务。</div>
      </div>
    </section>
  </div>`,
};

const JobDetailView = {
  components: { StatusTag, Seal },
  props: ["id"],
  setup(props) {
    const job = ref(null), log = ref(""), offset = ref(0), follow = ref(true), logEl = ref(null);
    let timer;
    async function tick() {
      try {
        job.value = await api("/jobs/" + props.id);
        const res = await api(`/jobs/${props.id}/log?offset=${offset.value}`, { raw: true });
        if (res.ok) {
          const text = await res.text();
          if (text) {
            log.value = (log.value + text).slice(-200000);
            offset.value = Number(res.headers.get("X-Log-Offset") || offset.value);
            if (follow.value) nextTick(() => { if (logEl.value) logEl.value.scrollTop = logEl.value.scrollHeight; });
          }
        }
        if (!["queued", "running", "cancelling"].includes(job.value.status)) clearInterval(timer);
      } catch (e) { clearInterval(timer); fail(e); }
    }
    async function cancel() {
      if (!confirm("停止这个任务？已经完成的股票会保留结果。")) return;
      try { await api(`/jobs/${props.id}/cancel`, { method: "POST" }); toast("正在停止"); tick(); } catch (e) { fail(e); }
    }
    onMounted(() => { tick(); timer = setInterval(tick, 2500); });
    onUnmounted(() => clearInterval(timer));
    const percent = computed(() => {
      const j = job.value; if (!j) return 0;
      if (j.status === "done") return 100;
      if (!j.progress) return j.status === "running" ? 3 : 0;
      if (j.progress.fraction != null) return Math.max(3, j.progress.fraction * 100);
      return Math.max(3, (j.progress.index - 1) / j.progress.total * 100);
    });
    return { job, log, follow, logEl, cancel, percent, when, duration, money, pct, numf, actionName, jobTitle, KIND };
  },
  template: `<div class="page" v-if="job">
    <div class="page-head">
      <div><a href="#/jobs" class="small">分析任务</a><h1 style="margin-top:4px">{{ jobTitle(job) }}</h1>
        <p>提交于 {{ when(job.created_at) }}<template v-if="job.started_at">，已用 {{ duration(job.started_at, job.finished_at) }}</template><template v-if="job.cost != null">，花费 ¥{{ money(job.cost) }}</template></p></div>
      <div class="row"><StatusTag :status="job.status"/>
        <button v-if="['queued','running'].includes(job.status)" class="btn danger small" @click="cancel">停止</button></div>
    </div>
    <div v-if="['queued','running','cancelling'].includes(job.status)" style="margin-bottom:18px">
      <div class="progress" role="progressbar" :aria-valuenow="Math.round(percent)" aria-valuemin="0" aria-valuemax="100"><i :style="{ width: percent + '%' }"></i></div>
      <p class="muted small" style="margin-top:6px">
        <template v-if="job.status === 'queued'">排队中，前面的任务完成后开始。</template>
        <template v-else-if="job.progress">正在分析第 {{ job.progress.index }}/{{ job.progress.total }} 只：{{ withName(job.progress.ticker) }}<template v-if="job.progress.step">
          <template v-if="job.progress.runs > 1">，第 {{ job.progress.run }}/{{ job.progress.runs }} 次运行</template>，{{ job.progress.step }}（{{ job.progress.step_done }}/{{ job.progress.step_total }} 步）</template></template>
        <template v-else>正在准备…</template></p>
    </div>
    <div v-if="job.status === 'failed'" class="note err" style="margin-bottom:18px">{{ job.error }}</div>

    <section v-if="job.result && job.result.runs && job.result.runs.length" style="margin-bottom:28px">
      <div class="panel"><ul class="board">
        <li v-for="(r, i) in job.result.runs" :key="i">
          <Seal :rating="r.rating"/>
          <div><Tk :t="r.ticker" big/><div class="meta">{{ r.trade_date }}<template v-if="r.vote_ratings">，投票 {{ r.vote_ratings }}</template></div></div>
          <div class="kv"><span>操作</span><strong style="font-family:var(--font);font-size:15px">{{ actionName(r.action) }}</strong></div>
          <div class="kv"><span>目标仓位</span><strong>{{ r.target_weight == null ? '—' : pct(r.target_weight) }}</strong></div>
          <div class="kv hide-s"><span>花费</span><strong>{{ r.cost == null ? '—' : '¥' + money(r.cost) }}</strong></div>
          <div><a v-if="r.report_dir" :href="'/api/jobs/' + job.id + '/report/' + i" target="_blank" rel="noopener">报告</a></div>
        </li></ul></div>
      <p v-if="job.result.missing && job.result.missing.length" class="note warn" style="margin-top:10px">没有得到结论：{{ job.result.missing.join('、') }}。原因见下面的日志。</p>
    </section>

    <section>
      <div class="row" style="justify-content:space-between;margin-bottom:8px"><h2>运行日志</h2>
        <label class="check small"><input type="checkbox" v-model="follow">自动滚到最新</label></div>
      <pre class="log" ref="logEl">{{ log || '还没有输出。' }}</pre>
    </section>
  </div>`,
};

// --- watchlist ---------------------------------------------------------------------------
const WatchlistView = {
  setup() {
    const items = ref([]), add = ref(""), busy = ref(false);
    async function load() { try { items.value = await api("/watchlist"); } catch (e) { fail(e); } }
    async function submit() {
      if (!add.value.trim()) return;
      busy.value = true;
      try {
        const r = await api("/watchlist", { method: "POST", body: { tickers: [add.value] } });
        toast(r.added.length ? "已加入：" + r.added.join("、") : "这些代码已经在自选股里");
        add.value = ""; load();
      } catch (e) { fail(e); } finally { busy.value = false; }
    }
    async function remove(t) {
      try { await api("/watchlist/" + encodeURIComponent(t), { method: "DELETE" }); toast("已移除 " + withName(t)); load(); } catch (e) { fail(e); }
    }
    const SOURCE = { manual: "手动添加", screen: "选股", import: "导入" };
    onMounted(load);
    return { items, add, busy, submit, remove, when, SOURCE };
  },
  template: `<div class="page">
    <div class="page-head"><div><h1>自选股</h1><p>每日定时分析和“发起分析”默认使用这里的股票。</p></div></div>
    <form class="row" @submit.prevent="submit" style="margin-bottom:16px">
      <input class="input num grow" style="max-width:420px" v-model="add" placeholder="代码，逗号分隔：600519.SS, 000960.SZ, 0700.HK, NVDA" aria-label="要添加的代码">
      <button class="btn primary" :disabled="busy || !add.trim()">添加</button>
    </form>
    <div class="panel table-wrap">
      <table class="t" v-if="items.length"><thead><tr><th>代码</th><th>来源</th><th>备注</th><th>加入时间</th><th></th></tr></thead>
        <tbody><tr v-for="w in items" :key="w.ticker">
          <td style="font-size:16px"><Tk :t="w.ticker"/></td><td class="muted small">{{ SOURCE[w.source] || w.source }}</td>
          <td class="muted small">{{ w.note || '—' }}</td><td class="muted small">{{ when(w.added) }}</td>
          <td class="r"><button class="btn quiet small" @click="remove(w.ticker)">移除</button></td></tr></tbody></table>
      <div v-else class="board-empty">自选股是空的。在上面输入代码，或去<a href="#/screen">选股</a>里挑选。</div>
    </div>
    <p class="muted small" style="margin-top:10px">代码格式：上交所 .SS，深交所 .SZ，港股 .HK（如 0700.HK），美股直接写代码。</p>
  </div>`,
};

// --- book: real holdings and the paper account -------------------------------------------
const BookView = {
  components: { EquityChart },
  setup() {
    const tab = ref("paper");
    const book = ref(null), cash = ref(null), currency = ref("CNY"), paper = ref(null), paperError = ref("");
    const draft = reactive({ ticker: "", quantity: null, average_price: null });
    const opening = reactive({ cash: 1000000, currency: "CNY", busy: false, restart: false });
    async function loadBook() {
      try { book.value = await api("/portfolio"); cash.value = book.value.cash; currency.value = book.value.currency; } catch (e) { fail(e); }
    }
    async function loadPaper() {
      paperError.value = "";
      try { paper.value = await api("/paper"); } catch (e) { paperError.value = e.message; }
    }
    async function openPaper() {
      if (opening.restart && !confirm(`重新开始模拟盘？现有持仓和现金会清空，按 ${money(opening.cash, 0)} ${opening.currency} 重新开始；以前的成交记录会另存保留。`)) return;
      opening.busy = true;
      try {
        paper.value = await api("/paper/open", { method: "POST", body: { cash: opening.cash, currency: opening.currency, restart: opening.restart } });
        toast(opening.restart ? "模拟盘已重新开始" : "模拟盘已开设，之后的分析结论会自动模拟下单");
        opening.restart = false;
      } catch (e) { fail(e); } finally { opening.busy = false; }
    }
    async function switchOrders(on) {
      try { const r = await api("/paper/switch", { method: "PUT", body: { enabled: on } }); paper.value.orders_on = r.orders_on; toast(on ? "已开启自动模拟下单" : "已暂停自动模拟下单"); }
      catch (e) { fail(e); }
    }
    async function saveCash() {
      try { await api("/portfolio/cash", { method: "PUT", body: { cash: cash.value === "" ? null : cash.value, currency: currency.value } }); toast("现金已保存"); } catch (e) { fail(e); }
    }
    async function saveHolding(h) {
      try { await api("/portfolio/holdings/" + encodeURIComponent(h.ticker), { method: "PUT", body: { quantity: Number(h.quantity), average_price: h.average_price === "" ? null : h.average_price } }); toast("已保存 " + withName(h.ticker)); loadBook(); } catch (e) { fail(e); }
    }
    async function addHolding() {
      if (!draft.ticker || !draft.quantity) return;
      await saveHolding({ ...draft, ticker: draft.ticker.trim().toUpperCase() });
      Object.assign(draft, { ticker: "", quantity: null, average_price: null });
    }
    async function removeHolding(t) {
      try { await api("/portfolio/holdings/" + encodeURIComponent(t), { method: "DELETE" }); toast("已删除 " + t); loadBook(); } catch (e) { fail(e); }
    }
    onMounted(() => { loadBook(); loadPaper(); });
    const v = computed(() => paper.value && paper.value.valuation);
    return { tab, book, cash, currency, paper, paperError, opening, v, draft, saveCash, saveHolding, addHolding, removeHolding,
             loadPaper, openPaper, switchOrders, money, pct, signed, numf, tone, when };
  },
  template: `<div class="page">
    <div class="page-head"><div><h1>持仓</h1><p>模拟盘按系统的结论自动下单；实盘持仓由你录入，用来计算仓位和止损。</p></div></div>
    <div class="tabs" role="tablist">
      <button role="tab" :aria-selected="tab === 'paper'" :class="tab === 'paper' && 'on'" @click="tab = 'paper'">模拟盘</button>
      <button role="tab" :aria-selected="tab === 'real'" :class="tab === 'real' && 'on'" @click="tab = 'real'">实盘持仓</button>
    </div>

    <template v-if="tab === 'paper'">
      <div v-if="paperError" class="note err row" style="justify-content:space-between">读取模拟盘失败：{{ paperError }}<button class="btn small" @click="loadPaper">重试</button></div>
      <div v-else-if="!paper" class="muted">正在读取…</div>
      <form v-else-if="!paper.exists || opening.restart" class="panel panel-body" @submit.prevent="openPaper" style="max-width:640px">
        <h2 style="margin-bottom:6px">{{ opening.restart ? '重新开始模拟盘' : '开设模拟盘' }}</h2>
        <p class="muted small" style="margin-bottom:16px">开设后，你发起的每次分析（包括定时分析）得出结论时，会按目标仓位生成一笔模拟订单，在下一个交易日开盘价成交：A 股 T+1，开盘涨停买不进、跌停卖不出，手续费和印花税按实际费率扣除，止损只上移不下移。</p>
        <div class="form-grid">
          <label class="field"><span>初始资金</span><input class="input num" type="number" min="1000" step="1000" v-model.number="opening.cash" required></label>
          <label class="field"><span>记账币种</span><select class="input" v-model="opening.currency"><option value="CNY">人民币 CNY</option><option value="HKD">港币 HKD</option><option value="USD">美元 USD</option></select></label>
        </div>
        <div class="row" style="margin-top:16px;justify-content:flex-end">
          <button v-if="opening.restart" type="button" class="btn quiet" @click="opening.restart = false">取消</button>
          <button class="btn primary" :disabled="opening.busy || !(opening.cash > 0)">{{ opening.restart ? '清空并重新开始' : '开设模拟盘' }}</button>
        </div>
      </form>
      <template v-else>
        <div class="row" style="justify-content:space-between;margin-bottom:16px">
          <label class="check"><input type="checkbox" :checked="paper.orders_on" @change="switchOrders($event.target.checked)">分析结论自动模拟下单</label>
          <span class="muted small">开设于 {{ paper.start_date || '—' }}，最近更新 {{ paper.last_update || '—' }}<button class="btn quiet small" style="margin-left:8px" @click="opening.restart = true; opening.currency = paper.currency">重新开始</button></span>
        </div>
        <div v-if="!paper.orders_on" class="note warn" style="margin-bottom:16px">自动下单已暂停：分析照常进行，但不会生成新的模拟订单；已有持仓的止损仍会执行。</div>
        <div class="split">
          <div class="panel panel-body"><EquityChart :rows="paper.equity" :benchmark="paper.benchmark"/></div>
          <aside class="facts" v-if="v && !v.error">
            <div class="fact"><span>总资产</span><div class="big">{{ money(v.equity, 0) }}</div><div class="sub">{{ paper.currency }}，起始 {{ money(paper.start_cash, 0) }}</div></div>
            <div class="fact"><span>累计收益</span><div class="big" :class="tone(v.return)">{{ signed(v.return) }}</div></div>
            <div class="fact"><span>现金</span><div class="big">{{ money(v.cash, 0) }}</div><div class="sub">持仓市值 {{ money(v.positions_value, 0) }}</div></div>
          </aside>
          <div v-else-if="v" class="note warn">{{ v.error }}</div>
        </div>
        <section class="section" v-if="v && v.rows && v.rows.length">
          <h2>持仓</h2>
          <div class="panel table-wrap"><table class="t wide"><thead><tr><th>代码</th><th class="r">数量</th><th class="r">成本</th><th class="r">现价</th><th class="r">盈亏</th><th class="r">市值</th><th class="r">占比</th><th class="r">止损</th></tr></thead>
            <tbody><tr v-for="r in v.rows" :key="r.ticker"><td><Tk :t="r.ticker"/></td><td class="r num">{{ numf(r.qty, 0) }}</td><td class="r num">{{ numf(r.avg_cost) }}</td>
              <td class="r num">{{ numf(r.close) }}</td><td class="r num" :class="tone(r.pnl)">{{ signed(r.pnl) }}</td><td class="r num">{{ money(r.value, 0) }}</td>
              <td class="r num">{{ pct(r.weight) }}</td><td class="r num">{{ numf(r.stop) }}</td></tr></tbody></table></div>
        </section>
        <section class="section" v-if="paper.pending && paper.pending.length">
          <h2>待成交订单</h2>
          <p class="muted small" style="margin-bottom:8px">下一个交易日开盘时按开盘价成交。</p>
          <div class="panel table-wrap"><table class="t wide"><thead><tr><th>代码</th><th>方向</th><th class="r">数量</th><th>依据</th><th class="r">止损</th></tr></thead>
            <tbody><tr v-for="o in paper.pending" :key="o.ticker"><td><Tk :t="o.ticker"/></td>
              <td :class="o.side === 'BUY' ? 'up' : 'down'">{{ o.side === 'BUY' ? '买入' : '卖出' }}</td><td class="r num">{{ numf(o.qty, 0) }}</td>
              <td class="small">{{ o.decision_date }} 的结论（{{ o.rating }}）</td><td class="r num">{{ numf(o.stop) }}</td></tr></tbody></table></div>
        </section>
        <div v-if="!(v && v.rows && v.rows.length) && !(paper.pending && paper.pending.length) && !paper.trades.length" class="note section">
          还没有模拟持仓。去<a href="#/jobs">分析</a>页发起一次分析，得出买入或增持结论后，这里会出现待成交订单。</div>
        <section class="section" v-if="paper.trades.length">
          <h2>最近成交</h2>
          <div class="panel table-wrap"><table class="t wide"><thead><tr><th>日期</th><th>代码</th><th>方向</th><th class="r">数量</th><th class="r">价格</th><th class="r">费用</th><th>状态</th><th>说明</th></tr></thead>
            <tbody><tr v-for="(t, i) in paper.trades" :key="i"><td class="num">{{ t.date }}</td><td><Tk :t="t.ticker"/></td>
              <td :class="(t.side || '').toUpperCase() === 'BUY' ? 'up' : 'down'">{{ (t.side || '').toUpperCase() === 'BUY' ? '买入' : '卖出' }}</td><td class="r num">{{ numf(t.qty, 0) }}</td>
              <td class="r num">{{ numf(t.price) }}</td><td class="r num">{{ numf(t.fees) }}</td><td class="small">{{ { filled: '成交', rejected: '未成交' }[t.status] || t.status }}</td><td class="muted small">{{ t.reason }}</td></tr></tbody></table></div>
        </section>
      </template>
    </template>

    <template v-else-if="book">
      <div class="panel panel-body">
        <h3 style="margin-bottom:10px">现金</h3>
        <form class="row" @submit.prevent="saveCash">
          <input class="input num" style="max-width:220px" type="number" step="any" v-model.number="cash" placeholder="未填写" aria-label="现金">
          <select class="input" style="max-width:110px" v-model="currency" aria-label="币种"><option>CNY</option><option>HKD</option><option>USD</option></select>
          <button class="btn">保存</button>
        </form>
      </div>
      <div class="panel table-wrap" style="margin-top:16px">
        <table class="t"><thead><tr><th>代码</th><th class="r">数量（股）</th><th class="r">成本价</th><th></th></tr></thead>
          <tbody>
            <tr v-for="h in book.holdings" :key="h.ticker"><td><Tk :t="h.ticker"/></td>
              <td class="r"><input class="input num" style="max-width:140px;text-align:right" type="number" step="any" v-model.number="h.quantity" :aria-label="h.ticker + ' 数量'"></td>
              <td class="r"><input class="input num" style="max-width:140px;text-align:right" type="number" step="any" v-model.number="h.average_price" :aria-label="h.ticker + ' 成本价'"></td>
              <td class="r"><button class="btn small" @click="saveHolding(h)">保存</button> <button class="btn quiet small" @click="removeHolding(h.ticker)">删除</button></td></tr>
            <tr><td><input class="input num" v-model="draft.ticker" placeholder="新代码" aria-label="新持仓代码"></td>
              <td class="r"><input class="input num" style="max-width:140px;text-align:right" type="number" step="any" v-model.number="draft.quantity" placeholder="数量" aria-label="新持仓数量"></td>
              <td class="r"><input class="input num" style="max-width:140px;text-align:right" type="number" step="any" v-model.number="draft.average_price" placeholder="成本价" aria-label="新持仓成本价"></td>
              <td class="r"><button class="btn primary small" @click="addHolding" :disabled="!draft.ticker || !draft.quantity">添加</button></td></tr>
          </tbody></table>
      </div>
      <p class="muted small" style="margin-top:10px">不填写持仓时，分析给出的是占总资产的仓位比例；填写后会换算成股数和手数。</p>
    </template>
  </div>`,
};

// --- data sources --------------------------------------------------------------------
const SourcesView = {
  setup() {
    const rows = ref([]), balance = ref(null);
    const STATE = { ok: ["正常", "done"], unused: ["未使用", "queued"], failing: ["出错", "warn"], "circuit open": ["暂停使用", "failed"], "not configured": ["未配置", "cancelled"] };
    async function load() { try { rows.value = (await api("/datasources")).sources; } catch (e) { fail(e); } }
    async function reset() { try { await api("/datasources/reset", { method: "POST" }); toast("已清除记录，所有来源会重新尝试"); load(); } catch (e) { fail(e); } }
    onMounted(async () => { load(); try { balance.value = await api("/balance"); } catch (e) { /* */ } });
    const groups = computed(() => {
      const g = []; let cur = null;
      for (const r of rows.value) { if (!cur || cur.capability !== r.capability) { cur = { capability: r.capability, title: r.title, rows: [] }; g.push(cur); } cur.rows.push(r); }
      return g;
    });
    const ago = s => s ? when(new Date(s * 1000).toISOString()) : "—";
    return { groups, balance, reset, STATE, ago, money, isAdmin: computed(() => store.me && store.me.role === "admin") };
  },
  template: `<div class="page">
    <div class="page-head"><div><h1>数据源</h1><p>每类数据按顺序尝试多个来源；连续失败 3 次的来源会暂停 10 分钟，然后自动重试。</p></div>
      <button v-if="isAdmin" class="btn" @click="reset">清除记录并全部重试</button></div>
    <div class="note" style="margin-bottom:18px" v-if="balance">
      <template v-if="balance.ok">DeepSeek 账户{{ balance.available ? '可用' : '暂不可用' }}：<span v-for="b in balance.balances.filter(x => x.total)" :key="b.currency" class="num">{{ b.currency }} {{ money(b.total) }} </span></template>
      <template v-else>DeepSeek 余额读取不到：{{ balance.error }}</template></div>
    <section v-for="g in groups" :key="g.capability" style="margin-bottom:22px">
      <h2 style="margin-bottom:8px">{{ g.title }}</h2>
      <div class="panel table-wrap"><table class="t"><thead><tr><th style="width:40px">顺序</th><th>来源</th><th>状态</th><th class="r">成功</th><th class="r">失败</th><th>最近成功</th><th class="r">耗时</th><th>说明</th></tr></thead>
        <tbody><tr v-for="r in g.rows" :key="r.provider">
          <td class="num muted">{{ r.rank }}</td><td>{{ r.label }}</td>
          <td><span class="tag" :class="(STATE[r.state] || [r.state, ''])[1]">{{ (STATE[r.state] || [r.state])[0] }}</span></td>
          <td class="r num">{{ r.ok }}</td><td class="r num">{{ r.fail }}</td><td class="small muted">{{ ago(r.last_ok) }}</td>
          <td class="r num small">{{ r.latency_ms == null ? '—' : r.latency_ms < 1000 ? r.latency_ms + ' ms' : (r.latency_ms / 1000).toFixed(1) + ' 秒' }}</td>
          <td class="small muted" style="max-width:340px">{{ r.note || (r.state !== 'ok' ? r.last_error : '') || '' }}</td></tr></tbody></table></div>
    </section>
  </div>`,
};

// --- settings ---------------------------------------------------------------------------
const SettingsView = {
  setup() {
    const sched = ref(null), pw = reactive({ old: "", new1: "", new2: "" }), users = ref([]);
    const nu = reactive({ username: "", password: "", role: "user", monthly_quota: null });
    const isAdmin = computed(() => store.me && store.me.role === "admin");
    const DAYS = [["mon-fri", "周一至周五"], ["*", "每天"], ["mon,wed,fri", "周一、三、五"]];
    async function load() {
      try { sched.value = await api("/schedule"); } catch (e) { fail(e); }
      if (isAdmin.value) try { users.value = await api("/admin/users"); } catch (e) { fail(e); }
    }
    async function saveSched() {
      try { sched.value = await api("/schedule", { method: "PUT", body: { enabled: sched.value.enabled, time_of_day: sched.value.time_of_day, days: sched.value.days, runs: sched.value.runs } }); toast(sched.value.enabled ? "定时分析已开启" : "定时分析已关闭"); }
      catch (e) { fail(e); }
    }
    async function changePw() {
      if (pw.new1 !== pw.new2) return toast("两次输入的新密码不一致", "err");
      try { await api("/auth/password", { method: "POST", body: { old_password: pw.old, new_password: pw.new1 } }); toast("密码已修改，请重新登录"); store.me = null; }
      catch (e) { fail(e); }
    }
    async function createUser() {
      try { await api("/admin/users", { method: "POST", body: { ...nu, monthly_quota: nu.monthly_quota || null } }); toast("已创建 " + nu.username); Object.assign(nu, { username: "", password: "", role: "user", monthly_quota: null }); load(); }
      catch (e) { fail(e); }
    }
    async function patch(u, body, msg) {
      try { await api("/admin/users/" + u.id, { method: "PATCH", body }); toast(msg); load(); } catch (e) { fail(e); }
    }
    function resetPw(u) { const p = prompt(`给 ${u.username} 设置新密码（至少 8 位）`); if (p) patch(u, { password: p }, "已重置 " + u.username + " 的密码"); }
    function setQuota(u) {
      const q = prompt(`${u.username} 每月模型花费上限（元），留空表示不限`, u.monthly_quota ?? "");
      if (q === null) return;
      patch(u, q.trim() === "" ? { clear_quota: true } : { monthly_quota: Number(q) }, "已更新额度");
    }
    onMounted(load);
    return { sched, pw, users, nu, isAdmin, DAYS, saveSched, changePw, createUser, patch, resetPw, setQuota, money, when };
  },
  template: `<div class="page">
    <div class="page-head"><div><h1>设置</h1></div></div>
    <section class="panel panel-body" v-if="sched">
      <h2 style="margin-bottom:6px">每日定时分析</h2>
      <p class="muted small" style="margin-bottom:14px">到点后把自选股排入分析任务。如果电脑上的 Windows 计划任务还在运行 18:30 的分析，这里请保持关闭，以免重复分析。</p>
      <form @submit.prevent="saveSched">
        <label class="check" style="margin-bottom:14px"><input type="checkbox" v-model="sched.enabled">开启定时分析</label>
        <div class="form-grid">
          <label class="field"><span>时间（北京时间）</span><input class="input num" type="time" v-model="sched.time_of_day" required></label>
          <label class="field"><span>哪几天</span><select class="input" v-model="sched.days"><option v-for="[k, v] in DAYS" :key="k" :value="k">{{ v }}</option></select></label>
          <label class="field"><span>每只运行几次</span><input class="input num" type="number" min="1" max="5" v-model.number="sched.runs"></label>
        </div>
        <div class="row" style="margin-top:14px;justify-content:space-between">
          <span class="muted small">{{ sched.next_run ? '下次运行：' + when(sched.next_run) : '' }}</span><button class="btn primary">保存</button></div>
      </form>
    </section>

    <section class="panel panel-body section">
      <h2 style="margin-bottom:14px">修改密码</h2>
      <form class="form-grid" @submit.prevent="changePw">
        <label class="field"><span>当前密码</span><input class="input" type="password" v-model="pw.old" autocomplete="current-password" required></label>
        <label class="field"><span>新密码（至少 8 位）</span><input class="input" type="password" v-model="pw.new1" autocomplete="new-password" minlength="8" required></label>
        <label class="field"><span>再输入一次</span><input class="input" type="password" v-model="pw.new2" autocomplete="new-password" minlength="8" required></label>
        <div class="field" style="justify-content:flex-end"><button class="btn">修改密码</button></div>
      </form>
      <p class="muted small" style="margin-top:10px">修改后所有设备都需要重新登录。</p>
    </section>

    <section class="section" v-if="isAdmin">
      <h2 style="margin-bottom:12px">账号管理</h2>
      <div class="panel table-wrap"><table class="t"><thead><tr><th>用户名</th><th>角色</th><th>状态</th><th class="r">本月花费</th><th class="r">月额度</th><th></th></tr></thead>
        <tbody><tr v-for="u in users" :key="u.id">
          <td>{{ u.username }}</td><td class="small">{{ u.role === 'admin' ? '管理员' : '普通用户' }}</td>
          <td><span class="tag" :class="u.is_active ? 'done' : 'cancelled'">{{ u.is_active ? '正常' : '已停用' }}</span></td>
          <td class="r num">¥{{ money(u.month_spend) }}</td><td class="r num">{{ u.monthly_quota == null ? '不限' : '¥' + money(u.monthly_quota) }}</td>
          <td class="r" style="white-space:nowrap"><button class="btn quiet small" @click="setQuota(u)">额度</button>
            <button class="btn quiet small" @click="resetPw(u)">重置密码</button>
            <button v-if="u.id !== $root.me.id" class="btn quiet small" @click="patch(u, { is_active: !u.is_active }, u.is_active ? '已停用 ' + u.username : '已启用 ' + u.username)">{{ u.is_active ? '停用' : '启用' }}</button></td></tr></tbody></table></div>
      <form class="panel panel-body" style="margin-top:14px" @submit.prevent="createUser">
        <h3 style="margin-bottom:12px">新建账号</h3>
        <div class="form-grid">
          <label class="field"><span>用户名</span><input class="input" v-model.trim="nu.username" required></label>
          <label class="field"><span>初始密码（至少 8 位）</span><input class="input" type="password" v-model="nu.password" minlength="8" autocomplete="new-password" required></label>
          <label class="field"><span>角色</span><select class="input" v-model="nu.role"><option value="user">普通用户</option><option value="admin">管理员</option></select></label>
          <label class="field"><span>每月额度（元，可空）</span><input class="input num" type="number" step="any" v-model.number="nu.monthly_quota"></label>
        </div>
        <div class="row" style="margin-top:14px;justify-content:flex-end"><button class="btn primary">新建账号</button></div>
      </form>
    </section>
  </div>`,
};

// --- "more" (phones: the pages the bottom bar has no room for) ---------------------------
const MoreView = {
  components: { Icon },
  setup() {
    async function logout() { try { await api("/auth/logout", { method: "POST" }); } catch (e) { /* */ } store.me = null; }
    return { logout, store };
  },
  template: `<div class="page">
    <div class="page-head"><div><h1>更多</h1><p>{{ store.me.display_name || store.me.username }}</p></div></div>
    <div class="panel nav" style="padding:6px">
      <a href="#/watchlist"><Icon name="watch"/>自选股</a>
      <a href="#/sources"><Icon name="sources"/>数据源</a>
      <a href="#/settings"><Icon name="settings"/>设置</a>
    </div>
    <button class="btn" style="margin-top:16px;width:100%" @click="logout">退出登录</button>
  </div>`,
};

// --- the shell ---------------------------------------------------------------------------
const NAV = [
  ["overview", "总览", "overview"], ["screen", "选股", "screen"], ["jobs", "分析", "jobs"],
  ["watchlist", "自选股", "watch"], ["book", "持仓", "book"], ["sources", "数据源", "sources"], ["settings", "设置", "settings"],
];
const MOBILE = [["overview", "总览", "overview"], ["screen", "选股", "screen"], ["jobs", "分析", "jobs"],
                ["book", "持仓", "book"], ["more", "更多", "settings"]];

const App = {
  components: { Icon, LoginView, OverviewView, ScreenView, JobsView, JobDetailView, WatchlistView, BookView, SourcesView, SettingsView, MoreView },
  setup() {
    onMounted(async () => {
      try { store.me = await api("/me"); } catch (e) { /* not signed in */ }
      store.ready = true;
      if (store.me) api("/balance").then(b => { store.balance = b; }).catch(() => {});
      refreshActive();
      setInterval(refreshActive, 6000);
    });
    watch(() => store.me, me => { if (me) refreshActive(); });
    const badge = k => k === "jobs" ? store.active.analysis : k === "screen" ? store.active.screen : 0;
    async function logout() { try { await api("/auth/logout", { method: "POST" }); } catch (e) { /* */ } store.me = null; }
    const view = computed(() => {
      const r = store.route;
      if (r.name === "jobs" && r.arg) return "JobDetailView";
      return { overview: "OverviewView", screen: "ScreenView", jobs: "JobsView", watchlist: "WatchlistView",
               book: "BookView", sources: "SourcesView", settings: "SettingsView", more: "MoreView" }[r.name] || "OverviewView";
    });
    const balanceText = computed(() => {
      const b = store.balance; if (!b || !b.ok) return "";
      const x = b.balances.find(y => y.total); return x ? `余额 ${x.currency === "CNY" ? "¥" : x.currency + " "}${money(x.total)}` : "";
    });
    const focusMain = () => { const m = document.getElementById("main"); if (m) m.focus(); };
    // a route change moves focus to the new page, so keyboard and screen-reader users start at its top
    watch(() => store.route, () => nextTick(focusMain));
    return { store, view, logout, NAV, MOBILE, balanceText, focusMain, badge, me: computed(() => store.me) };
  },
  template: `
    <template v-if="!store.ready"></template>
    <LoginView v-else-if="!store.me"/>
    <div v-else class="shell">
      <a class="skip" href="#main" @click.prevent="focusMain">跳到正文</a>
      <nav class="rail" aria-label="主导航">
        <div class="brand"><div class="brand-mark">研</div><div class="brand-name">TradingAgents<span>投研台</span></div></div>
        <div class="nav">
          <a v-for="[k, label, icon] in NAV" :key="k" :href="'#/' + k" :class="store.route.name === k && 'on'" :aria-current="store.route.name === k ? 'page' : null">
            <Icon :name="icon"/>{{ label }}<span v-if="badge(k)" class="badge tag running plain num" :aria-label="badge(k) + ' 个任务进行中'">{{ badge(k) }}</span></a>
        </div>
        <div class="rail-foot">
          <div class="who"><span>{{ store.me.display_name || store.me.username }}</span><button class="linkish small" @click="logout">退出</button></div>
          <div class="bal num">{{ balanceText }}</div>
        </div>
      </nav>
      <main class="main" id="main" tabindex="-1">
        <component :is="view" :key="store.route.name + (store.route.arg || '')" :id="store.route.arg"/>
      </main>
      <nav class="bottombar" aria-label="主导航">
        <a v-for="[k, label, icon] in MOBILE" :key="k" :href="'#/' + k" :class="(store.route.name === k || (k === 'more' && ['watchlist','sources','settings'].includes(store.route.name))) && 'on'"><Icon :name="icon"/>{{ label }}</a>
      </nav>
    </div>
    <div class="toasts" aria-live="polite"><div v-for="t in store.toasts" :key="t.id" class="toast" :class="t.kind" :role="t.kind === 'err' ? 'alert' : 'status'">{{ t.text }}</div></div>`,
};

const app = createApp(App);
app.config.globalProperties.location = window.location;
app.config.globalProperties.nameOf = nameOf;          // every page shows names next to codes
app.config.globalProperties.withName = withName;
app.component("Tk", Tk);
app.mount("#app");
})();
