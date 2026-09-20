# -*- coding: utf-8 -*-
"""
PaperIDE · AI 适配器层（P3-b / 2026-09-20 通用化）

规划 §5.1 拍板：**AI 层要留切换接口**——现在是 Claude Code，将来别人可以换 codex / DSH / 直连 API。
所以这一层只干三件事（别的什么都不干）：

    把 prompt + payload 喂进去  →  把 JSON 抠出来  →  交给校验器

**prompt 和校验器都不在这一层**（prompt 在 prompts/*.md，校验器是 P0 那个一字未改的）。
换后端只换这个文件里的一个类，prompt 和闸门原样不动。

## 两条路（配置里 `adapter` 选一条）

| | `openai`（**默认**） | `claude-code` |
|---|---|---|
| 怎么干活 | 打任意 OpenAI 兼容的 `/chat/completions` | 后台拉起本机 `claude` 无头进程 |
| 要什么 | **接口地址 + 模型名 + API Key** 三样 | 电脑里装了 Claude Code |
| 覆盖 | DeepSeek / Kimi / 智谱 / OpenAI / 本地 Ollama / 任何兼容服务 | 只有 CC 这一条 |

用户什么都不用先装：设置界面里选一个「常用服务」、贴上 Key 就能用
（`claude-code` 那条保留，给想用本机 CC 额度的人）。

实测配方（接续文档 §5.1，2026-09-17 在本机跑通）：
    claude -p <prompt> --output-format json --bare --strict-mcp-config --allowedTools ""
  · --bare 跳过 hooks/LSP/插件/CLAUDE.md/自动记忆 → 输入 48075 → 920 tokens，15s → 1.25s
  · 返回是信封，模型原话在 result 字段
  · ⚠ --bare 不加载 CLAUDE.md → **prompt 必须自包含**（铁律/词表/格式全写进 prompt 文件）
"""
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)                            # 直接跑本文件时也能 import core.*

# ★ 路径一律从 paths 取 —— **只读资源**（prompts/、配置）走 RES，
#   **用户数据**（.keys.json）走 DATA。打包后这两者不是同一个目录，
#   详见 core/paths.py 顶上那段。别在本文件里再拼 `ROOT/xxx`。
from core import paths                              # noqa: E402


class AIError(RuntimeError):
    pass


def _claude_exe():
    """Windows 上 `claude` 是 .cmd 包装，subprocess 直接按名字调不到（WinError 2）。
    用 shutil.which 把带扩展名的真路径找出来。"""
    import shutil
    for cand in ('claude', 'claude.cmd', 'claude.exe', 'claude.bat'):
        p = shutil.which(cand)
        if p:
            return p
    fallback = os.path.expanduser('~/AppData/Roaming/npm/claude.cmd')
    if os.path.exists(fallback):
        return fallback
    raise AIError('找不到 claude 命令（PATH 里没有，也没有 ~/AppData/Roaming/npm/claude.cmd）')


# ==================================================================== 抠 JSON
_FENCE = re.compile(r'```(?:json)?\s*(.*?)```', re.S)


def extract_json(text):
    """从模型回话里抠出 JSON。

    模型有很强的"加解释"倾向，哪怕三令五申。所以这里容错，按可靠性依次尝试：
      ① 整体就是 JSON
      ② ```json 代码块
      ③ **raw_decode 扫每个 {**——这是最要紧的一档：模型经常在 JSON 后面再补一段
         "说明"（`Extra data: line 15 column 12` 就是这个），或者前面写半句客套话。
         raw_decode 只吃第一个完整的 JSON 值，后面的赘余直接不管。
    抠不出来就抛——**不要**自作主张返回空对象，那会把错误藏起来。"""
    if not text or not text.strip():
        raise AIError('模型返回空')
    s = text.strip()
    try:
        return json.loads(s)
    except Exception:
        pass
    m = _FENCE.search(s)
    if m:
        try:
            return json.loads(m.group(1).strip())
        except Exception:
            pass
    dec = json.JSONDecoder()
    last = None
    for i, ch in enumerate(s):
        if ch not in '{[':
            continue
        try:
            obj, _end = dec.raw_decode(s[i:])
            if isinstance(obj, (dict, list)):
                return obj
        except Exception as e:
            last = e
    if last:
        raise AIError('抠出的片段不是合法 JSON：%s\n原文前 300 字：%s' % (last, s[:300]))
    raise AIError('回话里找不到 JSON。原文前 300 字：%s' % s[:300])


# ==================================================================== 适配器
class Adapter(object):
    name = 'base'

    def raw(self, prompt):
        raise NotImplementedError

    def json(self, prompt):
        return extract_json(self.raw(prompt))


class ClaudeCodeAdapter(Adapter):
    """后台拉起一个 Claude Code headless 进程（§5.1 拍板的方案）。

    ⚠ 2026-09-20 起**不再是默认**（默认走下面那条通用 API），但保留：
      有人愿意用本机 CC 的额度/账号，配置里把 `adapter` 写成 `claude-code` 即可。
    """
    name = 'claude-code'

    # 一节 2–4 千字的原文要出 1–3 张图，输出动辄好几千 token；实测单次 200–240s 是常态，
    # 300s 会在大节上超时（超时=白烧一次调用，还得重来）。给足 600s。
    def __init__(self, timeout=600, extra_args=()):
        self.timeout = timeout
        self.extra_args = list(extra_args)

    def raw(self, prompt):
        exe = _claude_exe()
        cmd = [exe, '-p', '--output-format', 'json', '--bare',
               '--strict-mcp-config', '--allowedTools', ''] + self.extra_args
        try:
            # cwd 给 DATA：`claude -p` 会把它当"当前项目"，而 RES 在打包后是
            # 临时解包目录（一个用户根本不知道在哪的地方），拿它当工作目录没意义。
            p = subprocess.run(cmd, input=prompt.encode('utf-8'), capture_output=True,
                               timeout=self.timeout, cwd=paths.DATA)
        except subprocess.TimeoutExpired:
            raise AIError('claude 调用超时（%ds）' % self.timeout)
        except FileNotFoundError:
            raise AIError('找不到 claude 命令（headless 适配器需要它在 PATH 里）')
        out = p.stdout.decode('utf-8', 'replace').strip()
        err = p.stderr.decode('utf-8', 'replace').strip()
        if not out:
            raise AIError('claude 没有输出。stderr：%s' % err[:400])
        try:
            env = json.loads(out)
        except Exception:
            # --output-format json 偶尔会被日志行弄脏，退化为整段当文本
            return out
        if env.get('is_error'):
            raise AIError('claude 报错：%s' % str(env.get('result'))[:300])
        self.last_usage = env.get('usage')
        self.last_cost = env.get('total_cost_usd')
        return env.get('result', '')


# ======================================================== OpenAI 兼容（通用）
#: 常见服务的预设。用户选一个 → URL/模型名自动填好，他只要贴 Key。
#: ⚠ 这些是**默认值不是白名单** —— 三个框都能自己改，任何兼容服务都能接。
#:   模型名会随厂商换代（实测 2026-09：Kimi 的 k2 系列 / moonshot-v1 已下线，
#:   现役是 kimi-k3；智谱是 glm-5）。填错了接口会明确报"模型不存在"，改一下就行。
PRESETS = {
    # ★ 2026-09-20 按官方文档（api-docs.deepseek.com/zh-cn）订正 + **实测过**：
    #   · 地址：文档给的是裸域名 `https://api.deepseek.com`（示例直接打 /chat/completions）。
    #     实测裸域名和 /v1 两种都能通，这里照文档写；chat_url() 会补成全路径。
    #   · 模型名：文档现在只有 `deepseek-flash` 和 `deepseek-v4-pro`，
    #     **`deepseek-chat` 已经不在文档里**（旧名还认，实测 200，会继续工作，只是别再写它）。
    #   · `thinking`：**flash 默认是开思考的**（文档：thinking.type 默认 enabled）。
    #     实测开了思考时，max_tokens=8 全被思考内容吃掉、正文为空且 finish=length ——
    #     而平台每节要吐几千 token 的 JSON，思考 token 会和正文**抢同一个 max_tokens**，
    #     长节会被误判成"截断"（那是硬错误，见下面 finish_reason=length 那段）。
    #     所以这里显式关掉：与平台既有的产出行为、速度、花费一致（temperature 也才生效）。
    #     想换成开思考：配置里 `ai.thinking` 写 "enabled"（见 extra_body 那段）。
    'deepseek': {'label': 'DeepSeek', 'base_url': 'https://api.deepseek.com',
                 'model': 'deepseek-flash',
                 'body': {'thinking': {'type': 'disabled'}}},
    'kimi': {'label': 'Kimi（月之暗面）', 'base_url': 'https://api.moonshot.cn/v1',
             'model': 'kimi-k3'},
    'zhipu': {'label': '智谱 GLM', 'base_url': 'https://open.bigmodel.cn/api/paas/v4',
              'model': 'glm-5'},
    'openai': {'label': 'OpenAI', 'base_url': 'https://api.openai.com/v1',
               'model': 'gpt-5-mini'},
    'ollama': {'label': '本地模型（Ollama）', 'base_url': 'http://localhost:11434/v1',
               'model': 'qwen3:8b'},
    'custom': {'label': '自定义', 'base_url': '', 'model': ''},
}

#: 这份配置的出厂默认预设（新装的人打开设置就看到它，贴个 Key 就能跑）
DEFAULT_PRESET = 'deepseek'

#: 本地服务普遍不校验 Key，但**要求头部在场**的也不少 —— 没填就给个占位串
_LOCAL_KEY = 'no-key-needed'
_LOCAL_RE = re.compile(r'^https?://(localhost|127\.0\.0\.1|0\.0\.0\.0|\[::1\])(:\d+)?', re.I)


def chat_url(base):
    """把用户填的"接口地址"拼成真正的 `/chat/completions` 全地址。

    用户会填成什么样子是没法规定的（各家文档写法都不一样），所以这里按最常见的三档收：
        https://api.deepseek.com/v1          → + /chat/completions
        https://open.bigmodel.cn/api/paas/v4 → + /chat/completions   （末尾是 v4 这种）
        https://xxx/chat/completions         → 原样用
    兜底：既没有版本段、也不是全路径的，补 `/v1/chat/completions`（OpenAI 的通行写法）。"""
    b = (base or '').strip().rstrip('/')
    if not b:
        return ''
    if b.endswith('/chat/completions'):
        return b
    if re.search(r'/v\d+[a-z]*$', b):          # /v1、/v4、/v1beta …
        return b + '/chat/completions'
    return b + '/v1/chat/completions'


#: HTTP 状态码 → 人话。设置面板上"测试连接"失败时直接给这句，别让用户猜。
_HTTP_HINTS = {
    400: '请求被拒（多半是模型名填错了，或者该服务不认某个参数）',
    401: 'API Key 不对或已失效',
    403: '这个 Key 没有调用该模型的权限',
    404: '接口地址不对（检查一下是不是少了 /v1，或者多了别的后缀）',
    429: '请求太频繁或额度用完了',
    500: '对方服务出错了，过一会儿再试',
    503: '对方服务暂时不可用',
}


class OpenAICompatAdapter(Adapter):
    """**通用的 OpenAI 兼容接口**（`POST <base>/chat/completions`）。

    ★ 2026-09-20 主人拍板：AI 层不再绑死某一家 —— 填「接口地址 + 模型名 + API Key」
      就能用，DeepSeek / Kimi / 智谱 / OpenAI / 本地 Ollama / 任何兼容服务都行。
      配置在 `paperide.config.json` 的 `ai` 段，Key 在 `.keys.json`（都不进版本库）。

    为什么不上官方 SDK：这一层只要"发一段 prompt、收一段 JSON"，`urllib` 就够了
    （少一个依赖、离线可用、打包体积小）。当初 DeepSeek 那条路就是这么验的。
    """
    name = 'openai'

    def __init__(self, preset=None, base_url=None, model=None, key=None, key_name=None,
                 timeout=240, temperature=0.3, json_mode=True, max_tokens=24576):
        cfg = ai_config()
        p = (preset or cfg['preset'] or DEFAULT_PRESET).strip()
        pre = PRESETS.get(p) or PRESETS['custom']
        self.preset = p
        #: 署名用哪个名字：**预设名**（deepseek / kimi …）而不是笼统的 `openai` ——
        #  graph.json 的 `generatedBy` 会写"… + <这个名字> 适配器"，老成果里写的就是
        #  `deepseek`，换了接口写法不该让署名跟着变（验收时按名字对得上才找得回来源）。
        self.name = p if p != 'custom' else 'openai'
        self.base_url = (base_url or cfg['base_url'] or pre['base_url']).strip()
        self.model = (model or cfg['model'] or pre['model']).strip()
        self.key_name = (key_name or cfg['key_name']
                         or (p if p != 'custom' else 'openai')).strip()
        # 本地服务不需要真 Key（Ollama / LM Studio / vLLM 都这样），别拿"没填 Key"卡住人
        #: 服务商特有的附加字段。预设可以带（如 DeepSeek 的 `thinking`），
        #: 用户配置里 `ai.thinking` 还能覆盖它 —— 各家参数不一样，**不预设就不发**，
        #: 不发就不会在别家那儿撞 400（这是"多接一家"最省事的做法）。
        self.extra_body = dict(pre.get('body') or {})
        _t = (cfg.get('thinking') or '').strip().lower()
        if _t in ('enabled', 'disabled'):
            self.extra_body['thinking'] = {'type': _t}
        self.local = bool(_LOCAL_RE.match(self.base_url))
        self.key = (key if key is not None else load_key(self.key_name)) or ''
        self.key = self.key.strip()
        if self.local and not self.key:
            self.key = _LOCAL_KEY
        self.timeout = timeout
        self.temperature = temperature
        self.json_mode = json_mode
        # ⚠ 输出上限这条线**踩过两次**：
        #   · 4096 时模型只吐完 lead 就被砍（views 一个都没有）；
        #   · 8192 时长节要 6 张图又顶到天花板（2026-09-18 实测 finish_reason=length）。
        # 2026-09-18 实测**接口本身远不止 8192**：同一个 key 打 16384 / 32768 都被接受
        #   实测接口远不止 8192（同一个 key 打 16384 / 32768 都被接受），瓶颈是我们自己配的数。
        # 现在给到 24576：够一节出 6 张图；再大也没意义（模型单轮不会吐那么多）。
        # timeout 同步提到 240s —— 输出越长生成越久，90s 会先超时。
        self.max_tokens = max_tokens
        # 结果明细（验收/日志用）
        self.last_usage = None
        self.last_finish = None
        self.last_url = chat_url(self.base_url)

        if not self.base_url:
            raise AIError('还没配 AI 接口地址 —— 点顶栏「设置」填上「地址 / 模型名 / Key」就能用'
                          '（一个都不用装，任何 OpenAI 兼容服务都行）')
        if not self.model:
            raise AIError('还没配模型名 —— 点顶栏「设置」填上「地址 / 模型名 / Key」就能用')
        if not self.key:
            raise AIError('还没配 API Key —— 点顶栏「设置」填一个就能用 AI 功能'
                          '（不填也能用：打开阅读 .ano、看章节分析与思想图都不受影响）')

    # ---------------------------------------------------------------- 请求体
    def _body(self, prompt):
        b = {
            'model': self.model,
            'messages': [{'role': 'user', 'content': prompt}],
            'temperature': self.temperature,
            'max_tokens': self.max_tokens,
        }
        if self.json_mode:
            b['response_format'] = {'type': 'json_object'}   # 支持结构化输出的服务会照做
        b.update(self.extra_body)
        return b

    def _relax(self, body, err):
        """接口报 400 时，**只针对它点名的那一个参数**降级一次。

        ⚠ 为什么不一上来就发最简 body：`max_tokens` 是**功能要的**（见下面
          finish_reason=length 那段，输出被截断 = 白烧一次调用）。能保就保 ——
          只有服务端**明说**这个参数它不认，才退一步。

        实测三种真实不兼容（2026-09 各家的现役模型）：
          · 推理类（OpenAI gpt-5 系）只认 `max_completion_tokens`，不认 `max_tokens`；
          · Kimi k3 / 部分国产模型**不接受 temperature**；
          · 本地小服务常常没有 `response_format`。
        """
        e = (err or '').lower()
        b = dict(body)
        if 'max_completion_tokens' in e and 'max_completion_tokens' in b:
            b.pop('max_completion_tokens', None)
            return b, '去掉 max_completion_tokens'
        if 'max_tokens' in e and 'max_tokens' in b:
            b['max_completion_tokens'] = b.pop('max_tokens')
            return b, 'max_tokens → max_completion_tokens'
        if 'temperature' in e and 'temperature' in b:
            b.pop('temperature', None)
            return b, '去掉 temperature'
        if 'response_format' in e and 'response_format' in b:
            b.pop('response_format', None)
            return b, '去掉 response_format'
        return None, ''

    def _post(self, url, headers, body):
        req = urllib.request.Request(url, data=json.dumps(body).encode('utf-8'),
                                     headers=headers)
        return json.load(urllib.request.urlopen(req, timeout=self.timeout))

    # ---------------------------------------------------------------- 主流程
    def raw(self, prompt):
        url = chat_url(self.base_url)
        headers = {'Content-Type': 'application/json'}
        if self.key:
            headers['Authorization'] = 'Bearer ' + self.key

        body = self._body(prompt)
        first_err = None
        relaxed = []
        for _ in range(4):
            try:
                r = self._post(url, headers, body)
                break
            except urllib.error.HTTPError as e:
                try:
                    text = e.read().decode('utf-8', 'replace')
                except Exception:
                    text = ''
                if first_err is None:
                    first_err = (e.code, text)
                if e.code != 400:                     # 400 以外没有"改参数就能好"的余地
                    raise self._http_error(e.code, text)
                body2, what = self._relax(body, text)
                if not body2:
                    raise self._http_error(e.code, text, relaxed)
                relaxed.append(what)
                body = body2
            except AIError:
                raise
            except Exception as e:
                raise AIError('调不到这个接口：%s: %s\n地址：%s'
                              '（检查地址拼写、本机网络/代理，本地模型还要确认服务已启动）'
                              % (type(e).__name__, e, url))
        else:
            code, text = first_err or (0, '')
            raise self._http_error(code, text, relaxed)

        self.last_usage = r.get('usage')
        # 有些网关/自建服务的错误是**塞在 200 的响应体里**的（HTTP 层完全看不出来），
        # 不拦的话会一路变成"模型返回空"这种没有信息量的报错 —— 把原文端出来给人看。
        if not r.get('choices'):
            raise AIError('接口没返回可用结果（HTTP 200，但没有 choices）：%s'
                          % str(r.get('error') or r)[:300])
        ch = r['choices'][0]
        self.last_finish = ch.get('finish_reason')
        # ⚠ **截断必须当成硬错误抛出来**：finish_reason=length 表示回话被 max_tokens 砍断了。
        #   以前不抛的话，下面 extract_json 的 raw_decode 会"成功"抠出**半截对象**——
        #   实测 2026-09-18：14622 字的 Results 要出 6 张图，回话被砍在 lead 之后，
        #   抠出来只有 {gist, focus}，于是判成"views 是空的"，连试 4 次全灭、这一节没图。
        #   抛出来，上层才能识别"是输出太长"并**换个小一点的规模重试**（见 pipeline_ai）。
        if self.last_finish == 'length':
            raise AIError('输出被 max_tokens(%d) 截断（finish_reason=length）：'
                          '这一版 JSON 不完整，不能当结果用' % self.max_tokens)
        msg = ch.get('message') or {}
        c = msg.get('content')
        # 少数服务按"内容块数组"回（新格式那一路），不是字符串 —— 拼起来，
        # 否则下游 extract_json 会拿到一个 list，报出来的是"回话里找不到 JSON"，查错方向全歪
        if isinstance(c, list):
            c = ''.join(p.get('text', '') for p in c if isinstance(p, dict))
        return c or ''

    def _http_error(self, code, text, relaxed=()):
        hint = _HTTP_HINTS.get(code, '对方接口报错了')
        extra = ('（已重试：%s，还是不行）' % '、'.join(relaxed)) if relaxed else ''
        body = (text or '').strip().replace('\n', ' ')[:300]
        return AIError('%s HTTP %s：%s%s\n地址：%s\n%s'
                       % (self.preset, code, hint, extra, chat_url(self.base_url), body))

    # ---------------------------------------------------------------- 连接自检
    def ping(self):
        """「测试连接」按钮用：发一句最短的话看通不通。返回 (ok, 人话说明)。

        刻意**不用 json_mode** —— 这里要验的是"地址/Key/模型名对不对"，
        加个 response_format 反而多一个可能失败的面。"""
        keep = self.json_mode
        self.json_mode = False
        try:
            t0 = time.time()
            txt = self.raw('ping').strip().replace('\n', ' ')[:40]
            ms = int((time.time() - t0) * 1000)
            return True, '连上了（%s · %d ms）%s' % (self.model, ms, ('：' + txt) if txt else '')
        except AIError as e:
            return False, str(e)
        except Exception as e:
            return False, '%s: %s' % (type(e).__name__, e)
        finally:
            self.json_mode = keep


class DeepSeekAdapter(OpenAICompatAdapter):
    """旧名字（`core/translate_ai.py` 曾经**写死**它）。

    现在它就是"预设 = deepseek"的通用适配器，留着是为了老代码/老文档 import 它时不炸。
    新代码一律走 `get_adapter()`。"""
    name = 'deepseek'

    def __init__(self, **kw):
        kw.setdefault('preset', 'deepseek')
        super().__init__(**kw)


#: 注册表：配置里 `adapter` 能写哪些值。
#: ⚠ PRESETS 里的名字（deepseek / kimi / …）也认——那是**旧配置的写法**（`adapter: deepseek`），
#:   由 get_adapter 兜住，见那里。
ADAPTERS = {'openai': OpenAICompatAdapter, 'claude-code': ClaudeCodeAdapter,
            'deepseek': DeepSeekAdapter}


#: 用户自己填的 key（**不进版本库**）。给普通人用的版本不能要求他去设环境变量。
#: ⚠ 必须在 DATA 下 —— 打包后写到 RES（临时解包目录）里的 key，**下次启动就没了**，
#:   用户会看到"我明明填过，怎么又要填"。
KEYS = paths.data('.keys.json')


def load_key(name='deepseek'):
    """取 API key：**先看用户在设置里填的，再看环境变量**。

    顺序是刻意的：填进设置里的应当赢过环境变量 —— 用户刚在界面上改了却"不生效"，
    是最让人困惑的一种体验。
    """
    if os.path.exists(KEYS):
        try:
            k = (json.load(open(KEYS, encoding='utf-8')).get(name) or '').strip()
            if k:
                return k
        except Exception:
            pass
    return (os.environ.get(name.upper() + '_API_KEY') or '').strip()


def save_key(name, value):
    """存/清一个 key。`value` 为空 = 删掉它。返回存完之后的 key（便于回显打码）。"""
    cfg = {}
    if os.path.exists(KEYS):
        try:
            cfg = json.load(open(KEYS, encoding='utf-8'))
        except Exception:
            cfg = {}
    v = (value or '').strip()
    if v:
        cfg[name] = v
    else:
        cfg.pop(name, None)
    with open(KEYS, 'w', encoding='utf-8') as fh:
        json.dump(cfg, fh, ensure_ascii=False, indent=1)
    try:
        os.chmod(KEYS, 0o600)          # 尽量收紧权限（Windows 上不保证生效，尽力而为）
    except Exception:
        pass
    return v


#: **出厂默认**配置（跟着程序走，打包后在 RES 里）
CONFIG_RES = paths.res('paperide.config.json')
#: 用户改过的配置（DATA）—— 有它就压过出厂那份
CONFIG_DATA = paths.data('paperide.config.json')


def config_path():
    """配置读哪个文件：**用户改过的优先**，没改过才读出厂那份。

    为什么分两份：出厂的默认值必须跟着程序走；但用户（或设置界面）改了之后，
    **改动得活过重装**。只写一份的话，写 RES 里 → 重装即丢；写 DATA 里 → 出厂默认又没了。
    分两份两个问题都没有。
    """
    return CONFIG_DATA if os.path.exists(CONFIG_DATA) else CONFIG_RES


#: 兼容旧调用点（server.py 读它）。**读**永远走 config_path()，
#: 所以这里给的是"用户那份"的路径，语义上不会误导。
CONFIG = CONFIG_DATA


def read_config():
    """把生效的配置读成 dict（读不出来就给空 dict，调用方各自取默认值）。"""
    p = config_path()
    if os.path.exists(p):
        try:
            return json.load(open(p, encoding='utf-8')) or {}
        except Exception:
            return {}
    return {}


def ai_config():
    """AI 接口那一段配置。**只回用户实际存了什么**，没存的一律空串 ——
    缺失的默认值由 `OpenAICompatAdapter` 拿预设补（那里才知道 preset 是什么）。

    兼容两代写法：
        旧： {"adapter": "deepseek"}                       ← 首日起就在用的那种
        新： {"adapter": "openai", "ai": {"preset": "deepseek",
              "base_url": "…", "model": "deepseek-flash", "key_name": "deepseek"}}
    """
    cfg = read_config()
    ai = dict(cfg.get('ai') or {})
    if not ai.get('preset'):
        a = (cfg.get('adapter') or '').strip()
        if a in PRESETS:                    # 旧写法：adapter 直接写成预设名
            ai['preset'] = a
            ai.setdefault('key_name', a)    # 旧 key 就存在这个名字下，别丢
    return {
        'preset': (ai.get('preset') or '').strip(),
        'base_url': (ai.get('base_url') or '').strip(),
        'model': (ai.get('model') or '').strip(),
        'key_name': (ai.get('key_name') or '').strip(),
        #: 覆盖预设的思考模式开关（"enabled"/"disabled"）。空 = 听预设的。
        'thinking': (ai.get('thinking') or '').strip().lower(),
    }


def save_ai(preset=None, base_url=None, model=None, key=None, key_name=None):
    """设置界面保存：接口地址 / 模型名 / Key。

    · 配置写 **DATA**（用户的选择得活过重装）；
    · Key 单独进 `.keys.json`（不进版本库、不回传前端）；
    · **Key 留空 = 不改动**（不是清空）—— 界面上那个框本来就故意不回填，
      人点"保存"时它必然是空的，当成清空就把人的 Key 删了。要清空请用 clear_key。
    """
    cfg = read_config()
    ai = dict(cfg.get('ai') or {})
    if preset:
        ai['preset'] = preset
    if base_url is not None:
        ai['base_url'] = (base_url or '').strip()
    if model is not None:
        ai['model'] = (model or '').strip()
    kn = (key_name or ai.get('key_name') or ai.get('preset') or 'openai').strip()
    if kn == 'custom':                      # 「自定义」统一用通用槽 —— 别为它另开一个，
        kn = 'openai'                       # 否则适配器那边按 'openai' 找、这边按 'custom' 存，
                                            # 用户会看到"我明明填了 Key 却说没配"（实测踩到过）
    ai['key_name'] = kn
    cfg['adapter'] = 'openai'               # 设置界面管的就是这条通用路
    cfg['ai'] = ai
    os.makedirs(os.path.dirname(CONFIG_DATA), exist_ok=True)
    with open(CONFIG_DATA, 'w', encoding='utf-8') as fh:
        json.dump(cfg, fh, ensure_ascii=False, indent=1)
    if key:                                 # 留空 = 不动（见 docstring）
        save_key(kn, key)
    return state()


def clear_key():
    """把当前这条配置的 Key 清掉（界面上"清空 Key"用）。"""
    ai = ai_config()
    save_key(ai.get('key_name') or 'openai', '')
    return state()


def state():
    """设置界面要的全部状态（**不回传 Key 本身**，只给打码提示）。"""
    cfg = ai_config()
    p = cfg['preset'] or DEFAULT_PRESET
    pre = PRESETS.get(p) or PRESETS['custom']
    base = cfg['base_url'] or pre['base_url']
    model = cfg['model'] or pre['model']
    kn = cfg['key_name'] or (p if p != 'custom' else 'openai')
    k = load_key(kn)
    #: 生效的思考模式：用户配置 > 预设带的 > 空（= 由服务端决定，界面上不显示）
    thinking = cfg.get('thinking') or ((pre.get('body') or {}).get('thinking') or {}).get('type') or ''
    return {
        'engine': adapter_name(),
        'thinking': thinking,
        'preset': p,
        'base_url': base,
        'model': model,
        'keyName': kn,
        'hasKey': bool(k),
        'keyHint': (k[:7] + '…' + k[-4:]) if len(k) > 14 else ('已配置' if k else ''),
        'local': bool(_LOCAL_RE.match(base or '')),
        #: 每一家有没有存过 Key。为什么要全给：用户在下拉里换一家时，"这一家我配过没有"
        #: 是当下最想知道的一件事 —— 只报当前那一家的话，换过去之后状态行是空白的，
        #: 他得先点一次保存才知道。**只报有没有，不报内容**（内容从不回传）。
        'keys': {pid: bool(load_key(pid)) for pid in PRESETS if pid != 'custom'},
        'presets': [{'id': pid, 'label': v['label'], 'base_url': v['base_url'],
                     'model': v['model']} for pid, v in PRESETS.items()],
        'url': chat_url(base),
    }


def set_adapter(name):
    """切换后端（老接口，留着给脚本/高级用户）。写进配置（下次起服务生效）。"""
    if name in PRESETS:
        cfg = read_config()
        ai = dict(cfg.get('ai') or {})
        ai['preset'] = name
        ai.setdefault('key_name', name)
        cfg['adapter'] = 'openai'
        cfg['ai'] = ai
        os.makedirs(os.path.dirname(CONFIG_DATA), exist_ok=True)
        json.dump(cfg, open(CONFIG_DATA, 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
        return name
    if name not in ADAPTERS:
        raise AIError('没有这个适配器：%s（可选 %s）' % (name, list(ADAPTERS) + list(PRESETS)))
    cfg = read_config()
    cfg['adapter'] = name
    os.makedirs(os.path.dirname(CONFIG_DATA), exist_ok=True)
    json.dump(cfg, open(CONFIG_DATA, 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
    return name


def adapter_name():
    """当前生效的后端名。优先级：环境变量 > 配置文件 > 默认（通用 API 那条）。"""
    name = (os.environ.get('PAPERIDE_ADAPTER') or '').strip()
    if not name:
        name = (read_config().get('adapter') or '').strip()
    name = name or 'openai'
    if name in PRESETS and name != 'custom':    # 旧写法 `adapter: deepseek`
        return 'openai'
    return name


def get_adapter(name=None, **kw):
    """造一个适配器实例。

    默认是 **openai**（通用 OpenAI 兼容接口）——2026-09-20 主人拍板：填「地址 + 模型名 + Key」
    就能用，谁都不用先装别的东西。想走本机 Claude Code 的把配置里的 `adapter` 写成
    `claude-code` 即可（那条路实测 ~220 秒/节，直连 API ~13 秒/节，快 17 倍）。

    `**kw` 透传给构造函数（`translate_ai` 要短超时、低温度那种），
    但**按签名过滤**：不是每个适配器都吃同样的参数（claude-code 就没有 temperature），
    直接塞过去会 TypeError。"""
    n = (name or adapter_name()).strip()
    cls = ADAPTERS.get(n)
    if cls is None and n in PRESETS:            # 旧配置：adapter 直接写成预设名
        cls, kw = OpenAICompatAdapter, dict(kw, preset=n)
    if cls is None:
        raise AIError('没有这个适配器：%s（可选 %s）' % (n, sorted(set(list(ADAPTERS) + list(PRESETS)))))
    if kw:
        import inspect
        ps = inspect.signature(cls.__init__).parameters
        kw = {k: v for k, v in kw.items() if k in ps}
    return cls(**kw)
