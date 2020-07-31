#!/usr/bin/env python

# php2jinja.py - Converts PHP to Jinja2 templates (experimental)
# Usage: php2jinja.py < input.php > output.html

import sys, argparse

sys.path.append('..')

from phply.phplex import lexer
from phply.phpparse import make_parser
from phply.phpast import *

parser = argparse.ArgumentParser(description='convert a PHP template to Jinja2')
parser.add_argument('input', metavar='TEMPLATE.php', nargs='?', type=str, help='name of PHP file to read (defaults to stdin)')
parser.add_argument('--out', dest='output', metavar="TEMPLATE.html", type=str, help='file to write Jinja2 to (defaults to stdout)')
parser.add_argument('--no-out', dest='dry_run', action='store_true', help='parse PHP but do not write out Jinja2')
parser.add_argument('--stubs', dest='stubs', metavar="STUBS.py", type=str, help='file to write function stubs to')
parser.add_argument('--line-statements', dest='use_line_statements', action='store_true', help='use line statements')

args = parser.parse_args()

input = open(args.input, "r") if args.input else sys.stdin
output = open(args.output, "w") if args.output else sys.stdout

called_functions = {}
indent_with = "\t"
loop_var_index = 1

# jinja tag generators 
# see https://jinja.palletsprojects.com/en/2.10.x/templates/#synopsis

def jstatement(*items, indent=0, inline=False, lstrip=False, rstrip=True):
    if not inline and args.use_line_statements:
        return '\n# '+ (indent_with * indent) + ' '.join(items) + '\n'
    return (indent_with * indent) + '{%' + (lstrip and '-' or '') + ' ' + ' '.join(items) + ' ' + (rstrip and '-' or '') + '%}'

def jexpr(body, indent=0, lstrip=False, rstrip=True):
    return (indent_with * indent) + '{{' + (lstrip and '-' or '') + ' ' + str(body) + ' ' + (rstrip and '-' or '') + '}}'

def jcomment(body):
    return '{# ' + str(body) + ' #}'

op_map = {
    '&&':  'and',
    '||':  'or',
    '!':   'not',
    '!==': '!=',
    '===': '==',
    '.':   '~',
}

# https://jinja.palletsprojects.com/en/2.10.x/templates/#literals
# "The special constants true, false, and none are indeed lowercase."
constant_map = {
    'null': 'none',
    'true': 'true',
    'false': 'false',
}

def unparse(nodes, join_with="\n"):
    return join_with.join(map(unparse_node, nodes))

def unparse_node(node, is_expr=False, indent=0):
    global loop_var_index

    if isinstance(node, (str, int, float)):
        return repr(node)

    if isinstance(node, InlineHTML):
        return str(node.data)

    if isinstance(node, Constant):
        return constant_map.get(node.name.lower(), str(node.name))

    if isinstance(node, Variable):
        return str(node.name[1:])

    if isinstance(node, Echo):
        # HACK: tack safe filter on the end in case the thing we're echoing is supposed to be HTML
        return jexpr('%s | safe' % (''.join(unparse_node(x, True) for x in node.nodes)))

    if isinstance(node, (Include, Require)):
        return '{%% include %s -%%}' % (unparse_node(node.expr, True))

    if isinstance(node, Block):
        return ''.join(unparse_node(x) for x in node.nodes)

    if isinstance(node, ArrayOffset):
        return '%s[%s]' % (unparse_node(node.node, True),
                           unparse_node(node.expr, True))

    if isinstance(node, ObjectProperty):
        return '%s.%s' % (unparse_node(node.node, True), node.name)

    if isinstance(node, Array):
        elems = []
        for elem in node.nodes:
            elems.append(unparse_node(elem, True))
        if node.nodes and node.nodes[0].key is not None:
            return '{%s}' % ', '.join(elems)
        else:
            return '[%s]' % ', '.join(elems)

    if isinstance(node, ArrayElement):
        if node.key:
            return '%s: %s' % (unparse_node(node.key, True),
                               unparse_node(node.value, True))
        else:
            return unparse_node(node.value, True)

    if isinstance(node, Assignment):
        if isinstance(node.node, ArrayOffset) and node.node.expr is None:
            return '{%% do %s.append(%s) -%%}' % (unparse_node(node.node.node, None),
                                                 unparse_node(node.expr, True))
        else:
            return jstatement("set", unparse_node(node.node, True), '=', unparse_node(node.expr, True))
            # return '{%% set %s = %s -%%}' % (unparse_node(node.node, True), unparse_node(node.expr, True))

    if isinstance(node, AssignOp):
        if node.op == '.=':
            variable = unparse_node(node.left, True)
            return '{%% set %s = %s ~ %s -%%}' % (variable, variable,
                                                 unparse_node(node.right, True))

    if isinstance(node, UnaryOp):
        op = op_map.get(node.op, node.op)
        return '(%s %s)' % (op, unparse_node(node.expr, True))

    if isinstance(node, BinaryOp):
        op = op_map.get(node.op, node.op)
        return '(%s %s %s)' % (unparse_node(node.left, True), op,
                               unparse_node(node.right, True))

    if isinstance(node, TernaryOp):
        return '(%s if %s else %s)' % (unparse_node(node.iftrue, True),
                                       unparse_node(node.expr, True),
                                       unparse_node(node.iffalse, True))

    if isinstance(node, IsSet):
        if len(node.nodes) == 1:
            return '(%s is defined)' % unparse_node(node.nodes[0], True)
        else:
            tests = ['(%s is defined)' % unparse_node(n, True)
                     for n in node.nodes]
            return '(' + ' and '.join(tests) + ')'

    if isinstance(node, Empty):
        return '(not %s)' % (unparse_node(node.expr, True))

    if isinstance(node, Silence):
        return unparse_node(node.expr, True)

    if isinstance(node, Cast):
        filter = ''
        if node.type in ('int', 'float', 'string'):
            filter = '|%s' % node.type
        return '%s%s' % (unparse_node(node.expr, True), filter)

    if isinstance(node, If):
        # sys.stderr.write(" ".join(map(str, (dir(node),))) + "\n");
        body = unparse_node(node.node)
        for elseif in node.elseifs:
            body += '\n{%% elif %s -%%}\n%s%s' % (unparse_node(elseif.expr, True), indent_with*(indent+1),
                                           unparse_node(elseif.node, indent=indent+1))
        if node.else_:
            body += '\n{%% else -%%}\n%s' % (unparse_node(node.else_.node, indent=indent+1))
        return '{%% if %s -%%}\n%s%s\n{%% endif -%%}' % (unparse_node(node.expr, True), indent_with*(indent+1),
                                                 body)

    if isinstance(node, While):
        # TODO: synthesize a better name for the variable based on the expr
        dummy = Foreach(node.expr, None, ForeachVariable('$loop_var_' + str(loop_var_index), False), node.node)
        loop_var_index += 1
        return unparse_node(dummy)

    if isinstance(node, Foreach):
        name = node.valvar.name
        if isinstance(name, Variable):
            name = name.name
        var = name[1:]
        if node.keyvar:
            var = '%s, %s' % (node.keyvar.name[1:], var)
        return '{%% for %s in %s -%%}%s{%% endfor -%%}' % (var,
                                                         unparse_node(node.expr, True),
                                                         unparse_node(node.node))

    if isinstance(node, Function):
        name = node.name
        params = []
        for param in node.params:
            params.append(param.name[1:])
            # if param.default is not None:
            #     params.append('%s=%s' % (param.name[1:],
            #                              unparse_node(param.default, True)))
            # else:
            #     params.append(param.name[1:])
        params = ', '.join(params)
        body = '\n    '.join(unparse_node(node) for node in node.nodes)
        return '{%% macro %s(%s) -%%}\n    %s\n{%%- endmacro -%%}\n\n' % (name, params, body)

    if isinstance(node, Return):
        # In a PHP template return aborts processing.  There is no clear analog return in Jinja2,
        # but could perhaps raise an exception?
        return jcomment(str(node))

    if isinstance(node, FunctionCall):
        # record this call in the global list so we can include it when generating stubs
        global called_functions
        called_functions.setdefault(node.name, []).append(node)
        params = [unparse_node(param.node, True) for param in node.params]
        if node.name.endswith('printf'):
            body = '%s %% (%s,)' % (params[0], ', '.join(params[1:]))
        else:
            if isinstance(node, StaticMethodCall):
                sys.stderr.write(str(dir(node)) + "\n")
            body = '%s(%s)' % (node.name, ', '.join(params))
        if is_expr:
            return body
        else:
            return jexpr(body)

    if isinstance(node, MethodCall):
        # TODO: record in called_functions
        params = [unparse_node(param.node, True) for param in node.params]
        body = '%s.%s(%s)' % (unparse_node(node.node, True), node.name, ', '.join(params))
        return body if is_expr else jexpr(body)

    if isinstance(node, StaticMethodCall):
        # TODO: record in called_functions
        params = ', '.join(unparse_node(param.node, True) for param in node.params)
        body = '%s.%s(%s)' % (node.class_, node.name, params)
        return body if is_expr else jexpr(body)

    if isinstance(node, New):
        # we just convert new into a function call
        dummy = FunctionCall(node.name, node.params, lineno=node.lineno)
        return unparse_node(dummy, is_expr=is_expr)

    if is_expr:
        return 'XXX(%r)' % str(node)
    else:
        return '{# XXX %s #}' % node

parser = make_parser()
php = input.read()
# HACK: replace any "else if" in the input with "elseif"
# they're essentially equivalent, but the lexer doesn't handle that yet
# and without this you end up with nested {% else %}{% if ... %} blocks
# https://www.php.net/manual/en/control-structures.elseif.php
php = php.replace('else if', 'elseif') # TODO use a safer regex
ast = parser.parse(php, lexer=lexer)
jinja = unparse(ast)
if args.stubs and called_functions:
    with open(args.stubs, "w") as stubsfile:
        stubsfile.write(f"# stubs for observed function calls\n")
        for name, nodes in called_functions.items():
            nodes = sorted(nodes, key=lambda n: len(n.params))
            longest_arity = nodes[-1]
            stubsfile.write(f"def {name}({', '.join(['arg'+str(i) for i,n in enumerate(longest_arity.params)])}):\n")
            if len(longest_arity.params) > 0:
                for example in list(set(map(lambda n: unparse_node(n, is_expr=True), nodes)))[:3]:
                    stubsfile.write(f"    # {example}\n")
            stubsfile.write(f"    pass\n\n")
if not args.dry_run:
    # HACK: clean up runs of blank lines
    jinja = jinja.replace("\n\n", "\n")
    output.write(jinja)
