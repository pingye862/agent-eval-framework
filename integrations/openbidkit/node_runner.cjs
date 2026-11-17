/* OpenBidKit 评测 Node runner。
 * stdin:  {"file_content": "...", "section_hint": "", "api": {"base_url","key","model","timeout"}}
 * stdout: {"ok":true,"content":"...","chat_calls":1,"model":"...","error":null}
 * 调用仓库真实代码 runInvalidBidAndRejectionItemsExtraction，注入 OpenAI 兼容 aiService。
 */
const path = require('node:path');

const SERVICES_DIR = path.join(__dirname, '..', 'repo', 'client', 'electron', 'services');
const { runInvalidBidAndRejectionItemsExtraction } = require(path.join(SERVICES_DIR, 'bidAnalysisTask.cjs'));

async function main() {
  let input;
  try {
    input = JSON.parse(require('node:fs').readFileSync(0, 'utf8'));
  } catch (e) {
    process.stdout.write(JSON.stringify({ ok: false, content: '', chat_calls: 0, error: 'stdin JSON 解析失败: ' + e.message }));
    return;
  }
  const { file_content, section_hint = '', api = {} } = input;
  const base = (api.base_url || 'https://api.deepseek.com/v1').replace(/\/+$/, '');
  const key = api.key || '';
  const model = api.model || 'deepseek-chat';
  const timeoutMs = api.timeout || 180000;

  let chatCalls = 0;
  const aiService = {
    getConfig() {
      // 控制分段阈值：评审场景下给足上下文，避免无谓分段合并
      return { contextLimit: 60000, maxOutputTokens: 8192 };
    },
    async chat({ messages, response_format }) {
      chatCalls += 1;
      const body = {
        model,
        messages,
        temperature: 0.2,
        ...(response_format ? { response_format } : {}),
      };
      const ctrl = new AbortController();
      const timer = setTimeout(() => ctrl.abort(), timeoutMs);
      try {
        const resp = await fetch(base + '/chat/completions', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json', Authorization: 'Bearer ' + key },
          body: JSON.stringify(body),
          signal: ctrl.signal,
        });
        if (!resp.ok) {
          const text = await resp.text().catch(() => '');
          throw new Error('LLM HTTP ' + resp.status + ': ' + text.slice(0, 300));
        }
        const data = await resp.json();
        return (data.choices && data.choices[0] && data.choices[0].message && data.choices[0].message.content) || '';
      } finally {
        clearTimeout(timer);
      }
    },
  };

  try {
    const content = await runInvalidBidAndRejectionItemsExtraction({
      aiService,
      fileContent: String(file_content || ''),
      sectionHint: section_hint || undefined,
    });
    process.stdout.write(JSON.stringify({ ok: true, content: String(content || ''), chat_calls: chatCalls, model, error: null }));
  } catch (e) {
    process.stdout.write(JSON.stringify({ ok: false, content: '', chat_calls: chatCalls, model, error: String(e && e.message || e) }));
  }
}

main();
