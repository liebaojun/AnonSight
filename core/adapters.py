# -*- coding: utf-8 -*-
"""
PaperIDE · AI 适配器层（P3-b）

规划 §5.1 拍板：**AI 层要留切换接口**——现在是 Claude Code，将来别人可以换 codex / DSH / 直连 API。
所以这一层只干三件事（别的什么都不干）：

    把 prompt + payload 喂进去  →  把 JSON 抠出来  →  交给校验器

**prompt 和校验器都不在这一层**（prompt 在 prompts/*.md，校验器是 P0 那个一字未改的）。
换后端只换这个文件里的一个类，prompt 和闸门原样不动。

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
    """后台拉起一个 Claude Code headless 进程（§5.1 拍板的方案）。"""
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


class DeepSeekAdapter(Adapter):
    """直连 DeepSeek 的纯 LLM API（§5.1 拍板：翻译走这条，便宜、稳、无进度焦虑）。

    ⚠ 不用 agent 那一层——翻译是"一问一答"，套 headless 进程是浪费。
    """
    name = 'deepseek'

    def __init__(self, model='deepseek-chat', timeout=240, temperature=0.3, json_mode=True,
                 max_tokens=24576):
        self.model = model
        self.timeout = timeout
        self.temperature = temperature
        self.json_mode = json_mode
        # ⚠ 输出上限这条线**踩过两次**：
        #   · 4096 时模型只吐完 lead 就被砍（views 一个都没有）；
        #   · 8192 时长节要 6 张图又顶到天花板（2026-09-18 实测 finish_reason=length）。
        # 2026-09-18 实测**接口本身远不止 8192**：同一个 key 打 16384 / 32768 都被接受
        #   （`deepseek-chat` 现在由 `deepseek-flash` 提供服务），所以瓶颈是我们自己配的数。
        # 现在给到 24576：够一节出 6 张图；再大也没意义（模型单轮不会吐那么多）。
        # timeout 同步提到 240s —— 输出越长生成越久，90s 会先超时。
        self.max_tokens = max_tokens
        self.key = load_key('deepseek')
        if not self.key:
            raise AIError('还没配 DeepSeek API Key —— 点顶栏「设置」填一个就能用 AI 功能'
                          '（不填也能用：打开阅读 .ano、看章节分析与思想图都不受影响）')

    def raw(self, prompt):
        body = {
            'model': self.model,
            'messages': [{'role': 'user', 'content': prompt}],
            'temperature': self.temperature,
            'max_tokens': self.max_tokens,
        }
        if self.json_mode:
            body['response_format'] = {'type': 'json_object'}   # DeepSeek 支持结构化输出
        req = urllib.request.Request(
            'https://api.deepseek.com/chat/completions',
            data=json.dumps(body).encode('utf-8'),
            headers={'Content-Type': 'application/json',
                     'Authorization': 'Bearer ' + self.key})
        try:
            r = json.load(urllib.request.urlopen(req, timeout=self.timeout))
        except urllib.error.HTTPError as e:
            raise AIError('DeepSeek HTTP %s：%s' % (e.code, e.read().decode('utf-8', 'replace')[:300]))
        except Exception as e:
            raise AIError('DeepSeek 调用失败：%s: %s' % (type(e).__name__, e))
        self.last_usage = r.get('usage')
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
        return ch['message']['content']


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


ADAPTERS = {'claude-code': ClaudeCodeAdapter, 'deepseek': DeepSeekAdapter}

#: **出厂默认**配置（跟着程序走，打包后在 RES 里）
CONFIG_RES = paths.res('paperide.config.json')
#: 用户改过的配置（DATA）—— 有它就压过出厂那份
CONFIG_DATA = paths.data('paperide.config.json')


def config_path():
    """配置读哪个文件：**用户改过的优先**，没改过才读出厂那份。

    为什么分两份：出厂的 `adapter: deepseek` 是给所有人用的默认值，必须跟着程序走；
    但用户（或 `set_adapter`）改了之后，**改动得活过重装**。只写一份的话，
    写 RES 里 → 重装即丢；写 DATA 里 → 出厂默认又没了。分两份两个问题都没有。
    """
    return CONFIG_DATA if os.path.exists(CONFIG_DATA) else CONFIG_RES


#: 兼容旧调用点（server.py 读它）。**读**永远走 config_path()，
#: 所以这里给的是"用户那份"的路径，语义上不会误导。
CONFIG = CONFIG_DATA


def set_adapter(name):
    """写进配置文件（下次起服务生效）。规划 §5.1 要求这一层可换，这是那个开关。"""
    if name not in ADAPTERS:
        raise AIError('没有这个适配器：%s（可选 %s）' % (name, list(ADAPTERS)))
    cfg = {}
    p = config_path()
    if os.path.exists(p):
        try:
            cfg = json.load(open(p, encoding='utf-8'))
        except Exception:
            cfg = {}
    cfg['adapter'] = name
    # 写到 DATA：这是**用户的**选择，得活过重装
    os.makedirs(os.path.dirname(CONFIG_DATA), exist_ok=True)
    json.dump(cfg, open(CONFIG_DATA, 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
    return name


def get_adapter(name=None):
    """优先级：显式参数 > 环境变量 > 配置文件 > 默认。

    默认是 **claude-code**（2026-09-17 主人拍板：后台拉起一个 Claude Code 新进程干总结/画图/标注）。
    但实测两条路的耗时差得很远——同一节、同一个模型（claude 在本机也路由到 DeepSeek）：

        claude-code  ~220 秒/节   （Claude Code 那套外壳的开销）
        deepseek      ~13 秒/节   （直连 API，快 17 倍，质量相当）

    所以这条开关是真有用的：想快就换 deepseek，想走拍板方案就保持默认。"""
    name = name or os.environ.get('PAPERIDE_ADAPTER')
    if not name:
        p = config_path()
        if os.path.exists(p):
            try:
                name = json.load(open(p, encoding='utf-8')).get('adapter')
            except Exception:
                name = None
    name = name or 'claude-code'
    if name not in ADAPTERS:
        raise AIError('没有这个适配器：%s（可选 %s）' % (name, list(ADAPTERS)))
    return ADAPTERS[name]()
