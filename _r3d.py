"""DATE-FIX: move the direct time note to the last prompt position."""
import ast

p = 'core/direct_runner.py'
src = open(p, encoding='utf-8').read()

# 1) remove the mid-prompt append
old_mid = (
    '        sys_p = ctx.get_effective_prompt_with_override("direct", ctx.active_preset, ctx.use_learned)\n'
    '        sys_p += _DIRECT_TIME_NOTE.format(dt=datetime.now().strftime("%Y-%m-%d %H:%M:%S"))'
)
new_mid = (
    '        sys_p = ctx.get_effective_prompt_with_override("direct", ctx.active_preset, ctx.use_learned)\n'
    '        # DATE-FIX (2026-10-06, telegram): the time note moved to the LAST\n'
    '        # position of the system prompt (see append before make_messages) -\n'
    '        # mid-prompt it sat before the tool/websearch notes and the small\n'
    '        # direct models ignored it, confidently claiming "tomorrow" was a\n'
    '        # year in the future.'
)
c1 = src.count(old_mid)
src = src.replace(old_mid, new_mid, 1)

# 2) append the sharpened final note at the first messages build
NL = chr(10)          # newline for python source strings
BS = chr(92)          # backslash
nn = BS + 'n' + BS + 'n'   # the two-character escape \n\n as SOURCE text

old_built = (
    '        messages = ctx.make_messages(ctx.pipeline, sys_p, direct_input, _direct_images, True, True, '
    'cached_mem_ctx=ctx.pipeline_mem_ctx, cached_sess_msgs=ctx.pipeline_sess_msgs)'
)
new_built = (
    '        # DATE-FIX (2026-10-06): LAST position + explicit tomorrow anchor +\n'
    '        # a direct anti-hallucination instruction - small models weight the\n'
    '        # prompt tail most and this is exactly the failure that produced\n'
    '        # "Oktober 2026 liegt mehr als ein Jahr in der Zukunft".\n'
    '        _now_dt = datetime.now()\n'
    '        _tomorrow_dt = datetime.fromtimestamp(_now_dt.timestamp() + 86400)\n'
    '        sys_p += (' + NL +
    '            "' + nn + '=== TODAY (authoritative, set by the system) ===' + nn + '"\n'
    '            f"Today is {_now_dt.strftime(' + chr(39) + '%A, %Y-%m-%d' + chr(39) + ')}. "\n'
    '            f"Tomorrow is {_tomorrow_dt.strftime(' + chr(39) + '%A, %Y-%m-%d' + chr(39) + ')}. "\n'
    '            f"Current local time: {_now_dt.strftime(' + chr(39) + '%H:%M' + chr(39) + ')}.' + BS + 'n"\n'
    '            "NEVER claim a date is in the future or the past without checking "\n'
    '            "this section. If asked about tomorrow, use this exact date."\n'
    '        )\n'
    '        messages = ctx.make_messages(ctx.pipeline, sys_p, direct_input, _direct_images, True, True, '
    'cached_mem_ctx=ctx.pipeline_mem_ctx, cached_sess_msgs=ctx.pipeline_sess_msgs)'
)
c2 = src.count(old_built)
src = src.replace(old_built, new_built, 1)

open(p, 'w', encoding='utf-8').write(src)
ast.parse(src)
print(f"mid removed: {c1} | final note: {c2} | syntax OK")
