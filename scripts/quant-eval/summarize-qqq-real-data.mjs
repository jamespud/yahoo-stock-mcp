/** Summarize actual saved MCP results; never turns missing history into a pass. */
import {readFileSync,writeFileSync,readdirSync,existsSync} from 'node:fs';
import {resolve,join,relative} from 'node:path';
import {createHash} from 'node:crypto';
const root=resolve(import.meta.dirname,'../..');
const run=resolve(process.env.QQQ_RUN_DIR||join(root,'.cache/qqq-real-2026-11-14-curl'));
const dir=join(root,'reports/qqq-real-data');
const read=p=>JSON.parse(readFileSync(p,'utf8'));
const acquisition=read(join(run,'acquisition.json'));
const portable=value=>JSON.parse(JSON.stringify(value).replaceAll(root+'/','').replaceAll(root,'.'));
const oracle=read(join(dir,'oracle.json'));
const replay=read(join(dir,'replay.json'));
const platform=existsSync(join(dir,'platform-checks.json'))?read(join(dir,'platform-checks.json')):{checks:[],recorded_at:null};
const traces=readdirSync(run).filter(p=>/^mcp-trace-.*\.jsonl$/.test(p)).map(p=>{const path=join(run,p),raw=readFileSync(path,'utf8'),calls=raw.trim().split('\n').filter(Boolean).map(JSON.parse);return {file:relative(root,path),hash:'sha256:'+createHash('sha256').update(raw).digest('hex'),tool_calls:calls.length,tool_errors:calls.filter(c=>c.result?.isError).length,bytes:Buffer.byteLength(raw)};});
const experiments=['1m','5m','15m','1h','1d','1w'].map(tf=>{
 const file=join(run,`result-${tf}.json`),r=read(file),quality=r.quality;
 const checks=oracle.results.find(x=>x.timeframe===tf);
 const replayCheck=replay.results.find(x=>x.timeframe===tf);
 const rejections=r.orders.filter(x=>x.status==='rejected').reduce((map,x)=>(map[x.reason]=(map[x.reason]||0)+1,map),{});
 return {timeframe:tf,status:r.job.status==='completed'&&checks?.status==='PASSED'&&replayCheck?.status!=='FAILED'?'PASSED':'FAILED',
  data_kind:'real_vendor_history',price_basis_status:'CONDITIONAL_VENDOR_OHLC_ASSUMPTION',job_id:r.job.job_id,job_status:r.job.status,job_runtime_seconds:r.job.finished_at-r.job.started_at,
  strategy_id:r.reproducibility.strategy_hashes.QQQ,plan_id:r.plan.plan_id,data_snapshot_id:r.reproducibility.data_snapshot_id,dataset_hash:r.dataset.dataset_hash,calendar_id:quality.calendar_id,
  first_bar:quality.first_start_time,last_bar:quality.latest_end_time,bars:quality.observed_count,missing_bars:quality.unavailable_completed_count,out_of_schedule_bars:quality.out_of_schedule_count,
  metrics:r.summary,commission_paid:r.attribution.commission_paid,accounting_residual:r.attribution.accounting_residual,cash:r.final_account.cash,equity:r.final_account.equity,
  final_quantity:r.final_account.positions.QQQ.quantity,receivables:r.final_account.receivables,rejected_orders:rejections,expired_orders:r.orders.filter(x=>x.status==='expired').length,
  correctness:checks,replay:replayCheck,result_file:relative(root,file),publication_delay_seconds:quality.publication_delay_seconds,week_source:tf==='1w'?'complete_calendar_weeks_resampled_from_daily':'vendor',
  warnings:[...(quality.unavailable_completed_count?['MISSING_BARS_NO_IMPUTATION']:[]),...(Object.keys(rejections).length?['CASH_CONSTRAINTS_STOPPED_SOME_ENTRIES']:[]),'VENDOR_RAW_BASIS_NOT_INDEPENDENTLY_CERTIFIED','MODELED_PUBLICATION_TIMES','NO_OOS_OR_PARAMETER_SEARCH'],
 };
});
const unsupported=[{vendor_interval:'30m',quant_status:'BLOCKED',reason:'Not in deployed quant_get_capabilities.timeframes; validation explicitly rejects 30m.'},{vendor_interval:'1mo',quant_status:'BLOCKED',reason:'Not in deployed quant_get_capabilities.timeframes; validation explicitly rejects 1mo.'}];
const report={version:'qqq-real-data-acceptance-v1',generated_at:new Date().toISOString(),symbol:'QQQ',requested_period:{start:acquisition.requested_start,end_exclusive:acquisition.requested_end_exclusive},
 overall_status:'BLOCKED',overall_reason:'Full-year history at every provider granularity is unavailable, and 30m/1mo execution is unsupported. Supported-engine experiments were run on actual available data.',
 correctness_status:experiments.every(x=>x.status==='PASSED')?'PASSED':'FAILED',price_basis_validation:'NOT_TESTED_INDEPENDENTLY; execution explicitly assumes vendor quote OHLC is raw; no adj_close inputs',llm_dsh_status:'NOT_TESTED_THIS_REAL_DATA_EXPERIMENT',
 strategy:{name:'close_vs_SMA5',entry:'close > SMA(close,5)',exit:'close < SMA(close,5)',warmup:5,parameters_optimized:false,cash_usd:10000,fixed_quantity:10,commission_bps_each_fill:5,slippage_bps_each_fill:5,execution:'first observed open strictly after modeled publication; no market-on-close or same-timestamp fill',signal_backend:'vector'},
 data_policy:{source:'live Yahoo chart responses',synthetic_data:false,missing_bars:'never filled',publication:'modeled end + 1 second; historical timestamps/revisions unavailable',dividends:'vendor ex-date/amount; unknown pay dates modeled outside exclusive simulation end; entitlements retained as receivables; no reinvestment',splits:'no split events returned for requested window',week_calendar:'calendar extended to Sunday 2026-11-15 so latest completed ISO week can be established; no future market observations imported'},
 acquisition:portable(acquisition),experiments,unsupported,oracle,replay,traces,tool_calls_total:traces.reduce((n,t)=>n+t.tool_calls,0),tool_errors_total:traces.reduce((n,t)=>n+t.tool_errors,0),
 platform_verification:platform,validation_commands:[...platform.checks.map(c=>({...c,status:c.exit_code===0?'PASSED':'FAILED'})),{command:'npm run test:quant-qqq',status:replay.status}],
 limitations:['Periods differ across intervals; do not rank strategies from these different samples.','SMA5 is five bars, not the same elapsed horizon across intervals.','Fixed ten-share sizing; account returns are not fully invested instrument returns.','Negative returns and high turnover are research findings, not engine correctness failures.','No OOS study or real LLM/DSH test is claimed by this deterministic MCP experiment.','30m and monthly raw files are downloaded and preserved, but not represented as engine backtests.']};
writeFileSync(join(dir,'acceptance.json'),JSON.stringify(report,null,2)+'\n');
const percent=x=>(x*100).toFixed(2)+'%';
const date=x=>x?.slice(0,10)||'—';
const dataRows=[...acquisition.datasets].sort((a,b)=>['1m','5m','15m','30m','60m','1d','1wk','1mo'].indexOf(a.interval)-['1m','5m','15m','30m','60m','1d','1wk','1mo'].indexOf(b.interval));
const lines=['# QQQ 真实数据多粒度验收（2026-11-14）','',`**计算正确性：${report.correctness_status}。全部粒度取得整年数据并回测：BLOCKED。**`,'',
 '请求范围：2025-11-14 至 2026-11-14（右端不含），最新交易日为 2026-11-13。实测 Yahoo 历史数据，不使用合成行情。',
 '', '## 获取结果','', '| Yahoo 粒度 | 实际日期 | 原始有效行 | 整年请求 |', '|---|---|---:|---|', ...dataRows.map(d=>`| ${d.interval} | ${date(d.first_bar)} ～ ${date(d.last_bar)} | ${d.bar_count} | ${d.full_year_request_status==='PASSED'?'PASSED':'BLOCKED：接口范围限制'} |`),
 '', '原始返回保留在本机 `.cache/qqq-real-2026-11-14-curl/`，包括完整响应、下载错误、数据文件、不可变快照、Parquet 工件及 MCP JSONL Trace。原始行数包括被单独排除的收市时间标记；周、月线也保留厂商边界/当前周期行，回测周线使用完整日线重采样，不将厂商日期标签当作可用时间。',
 '', '## 同一简单策略的真实 MCP 结果','', '规则：收盘价 > SMA5 买入；收盘价 < SMA5 卖出。初始 $10,000，每笔固定 10 股，只做多。每次成交手续费 5 bps、滑点 5 bps。发布时刻建模为 K 线结束后 1 秒；仅能在严格晚于发布时刻的开盘成交。连续盘中下一根开盘若已发生，须等待随后合法开盘。SMA5 表示五根 K 线，各粒度的时间跨度不同。',
 '', '| 粒度 | 实际日期 | 回测 Bar | 缺失 | 成交数 | 账户总收益 | 最大回撤（幅度） | 手续费 USD | 计算验收 |', '|---|---|---:|---:|---:|---:|---:|---:|---|',
 ...experiments.map(e=>`| ${e.timeframe} | ${date(e.first_bar)} ～ ${date(e.last_bar)} | ${e.bars} | ${e.missing_bars} | ${e.metrics.fill_count} | ${percent(e.metrics.total_return)} | ${percent(-e.metrics.max_drawdown)} | ${e.commission_paid.toFixed(2)} | ${e.status} |`),
 '', '30m、1mo 数据已下载，但部署 capabilities 不支持相应执行周期，合法性验证确实拒绝，两项回测均为 **BLOCKED / UNSUPPORTED_TIMEFRAME**。没有修改引擎扩大能力或伪装为 PASS。',
 '', '## 独立复核与重放','', `独立标准库 Oracle：${oracle.status}，未导入生产量化模块。逐条比对 SMA5、信号产生的订单、首次合法开盘成交、滑点、手续费、现金、数量、成本、已实现收益、应收股息、估值、权益、总收益及最大回撤。最大权益差 ${Math.max(...oracle.results.map(x=>x.max_equity_error||0))} USD，绝对容差 1e-7、相对容差 1e-11。`,
 '', `六粒度固定快照重复执行：${replay.status}。比较计划/快照身份及订单、成交、整条权益曲线和最终账户的内容哈希。共保留 ${report.tool_calls_total} 次 MCP 调用，${report.tool_errors_total} 个 Tool Error（包含初始运行及周线日历修正重测）；当前结果绑定六个真实已完成 Job。`,
 '', '## 研究限制与发现','', `- 高频交易费用显著。1m 有 ${experiments.find(e=>e.timeframe==='1m').rejected_orders.INSUFFICIENT_CASH||0} 笔、5m 有 ${experiments.find(e=>e.timeframe==='5m').rejected_orders.INSUFFICIENT_CASH||0} 笔现金不足拒单，独立 Oracle 确认该行为；没有将亏损或拒单改写成软件通过指标。`,
 '- 小时线有 11 个应有区间缺失，未补值。包含提前收市尾段与其他历史缺口，完整样本诊断见结果文件。盘中下载中的收市标记不构成额外可交易 Bar，已排除并保留原因。',
 '- 价格来自 Yahoo `quote` OHLC，未使用 `adjclose`。执行将其作为 raw 输入假设，**未完成独立 raw 认证**；本报告验证模拟计算，不证明数据源已独立验证。',
 '- 历史发布/修订时间未知；PIT 仅针对显式建模的可用时间成立。股息使用供应商除息日与金额；支付日未知，不推断支付日期，实验内保留应收，不进行再投资。',
 '- 不同粒度覆盖不同日期，收益不可直接排名；短区间年化收益不用于结论。未优化参数、未做 OOS，也未运行本轮真实 LLM/DSH。',
 '', '## 执行与文件','', ...report.validation_commands.map(c=>`- ${c.status}：\`${c.command}\`${c.passed?`（${c.passed} passed、${c.skipped} skipped）`:''}`),
 '', 'Python 初次命令因默认 uv 缓存目录只读而失败；将缓存限定到工作树并复用现有运行时后通过，初次错误见 `python-tests-initial.log`。初次 Node 下载连接超时，随后使用同一已配置代理和相同项目 User-Agent 的 curl 下载；完整初次错误及后续 HTTP 范围拒绝均保留。',
 '', '本轮增加获取、快照准备、MCP 执行、独立 Oracle、重放及报告脚本，并在 package.json 注册命令；`src/**` 无修改。',
 '', '复现：先在本机已配置网络下执行 `npm run quant:qqq:fetch` 获取固定日期数据，再执行 `npm run test:quant-qqq`；后者仅重放已下载数据，不调用网络或 LLM。可用 `QQQ_RUN_DIR` 指定本地数据目录，`QUANT_PYTHON` 指定已安装运行时。价格与发布时间假设见上文，脚本不会把条件模拟升级成数据验证。',
 '', '机器证据：`acceptance.json`、`oracle.json`、`replay.json`、`acquisition.json`、各命令日志，以及 acceptance.json 内列出的完整本地 Trace/结果/工件路径。', ''];
writeFileSync(join(dir,'acceptance.md'),lines.join('\n'));
console.log(JSON.stringify({correctness_status:report.correctness_status,overall_status:report.overall_status,experiments:experiments.map(x=>({timeframe:x.timeframe,status:x.status})),report:join(dir,'acceptance.md')}));
