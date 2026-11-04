"""Independently verify supplier formula caches without executing Excel code.

Only bounded arithmetic, local cell references and a small set of numeric
functions are evaluated. External/dynamic/unknown functions never fall back to
an unverified cache. Source workbooks are read, never recalculated in place.
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation, ROUND_HALF_UP, localcontext
import re
from xml.etree import ElementTree as ET
from zipfile import ZipFile

from openpyxl.formula.tokenizer import Tokenizer
from openpyxl.utils.cell import range_boundaries

from .models import WorkflowError

S = '{http://schemas.openxmlformats.org/spreadsheetml/2006/main}'
R = '{http://schemas.openxmlformats.org/officeDocument/2006/relationships}'
IMAGE_FORMULA = re.compile(r'^=?(?:_xlfn\.)?DISPIMG\(\s*"([^"\r\n]{1,200})"\s*[,;]\s*1\s*\)$', re.I)


def sheet_parts(archive):
    rels = {r.attrib['Id']: r.attrib['Target'] for r in ET.fromstring(archive.read('xl/_rels/workbook.xml.rels'))}
    root = ET.fromstring(archive.read('xl/workbook.xml'))
    return {s.attrib['name']: (rels[s.attrib[R+'id']].lstrip('/') if rels[s.attrib[R+'id']].startswith('/')
                             else 'xl/' + rels[s.attrib[R+'id']]) for s in root.findall(S+'sheets/'+S+'sheet')}


def formula_caches(path):
    result = {}
    with ZipFile(path) as archive:
        for title, part in sheet_parts(archive).items():
            with archive.open(part) as handle:
                for _, cell in ET.iterparse(handle, events=('end',)):
                    if cell.tag != S+'c':
                        continue
                    if cell.find(S+'f') is not None:
                        value = cell.find(S+'v')
                        raw = value.text if value is not None else None
                        kind = cell.attrib.get('t', 'n')
                        if raw is not None and kind == 'n':
                            try:
                                raw = Decimal(raw)
                            except InvalidOperation:
                                raw = None
                        elif raw is not None and kind == 'b':
                            raw = raw == '1'
                        result[title, cell.attrib['r']] = (kind, raw)
                    cell.clear()
    return result


def value_text(value):
    if value is None:
        return ''
    if isinstance(value, Decimal):
        if not value.is_finite() or abs(value.adjusted()) > 1000:
            raise ValueError('计算结果超出有效数值范围')
        return format(value, 'f').rstrip('0').rstrip('.') if '.' in format(value, 'f') else format(value, 'f')
    if isinstance(value, bool):
        return 'true' if value else 'false'
    return str(value)


class FormulaValues:
    def __init__(self, book, path):
        self.book = book
        self.caches = formula_caches(path)
        self.values = {}
        self.visiting = set()
        self.reports = {}
        self.steps = 0

    @staticmethod
    def number(value):
        if value in (None, ''):
            return Decimal(0)
        if isinstance(value, bool):
            return Decimal(int(value))
        if isinstance(value, (int, float, Decimal, str)):
            try:
                result = Decimal(str(value))
                if result.is_finite():
                    return result
            except InvalidOperation:
                pass
        raise ValueError('引用值不能用于数值计算')

    def cell(self, sheet, coordinate):
        self.steps += 1
        if self.steps > 2_000_000:
            raise ValueError('公式计算量超过当前上限')
        key = (sheet, coordinate.replace('$', '').upper())
        if key in self.values:
            return self.values[key]
        if key in self.visiting or len(self.visiting) >= 64:
            raise ValueError('公式循环引用或引用层数过深')
        if sheet not in self.book.sheetnames:
            raise ValueError('公式引用的工作表不存在')
        cell = self.book[sheet][key[1]]
        if cell.data_type == 'e':
            raise ValueError('公式引用了错误单元格 ' + coordinate)
        if cell.data_type != 'f':
            return cell.value
        formula = cell.value
        if not isinstance(formula, str) or IMAGE_FORMULA.fullmatch(formula):
            raise ValueError('图片或数组公式不能作为计算数值')
        self.visiting.add(key)
        try:
            with localcontext() as ctx:
                ctx.prec = 50
                value = NumericExpression(self, sheet, formula).parse()
                text = value_text(value)
            kind, cached = self.caches.get(key, ('n', None))
            equal = False
            if cached is not None and kind != 'e':
                if isinstance(value, Decimal):
                    try:
                        equal = abs(value - self.number(cached)) <= Decimal('1e-12') * max(Decimal(1), abs(value))
                    except ValueError:
                        pass
                else:
                    equal = value == cached
            self.reports[key] = {'cell': key[1], 'formula': formula, 'cached_value': value_text(cached), 'value': text,
                                 'status': 'cache_verified' if equal else 'cache_missing_recomputed' if cached is None else 'cache_corrected'}
            self.values[key] = value
            return value
        finally:
            self.visiting.remove(key)

    def resolve(self, sheet, coordinate):
        try:
            return value_text(self.cell(sheet, coordinate))
        except (ValueError, ArithmeticError, IndexError, RecursionError) as exc:
            raise WorkflowError(f'{sheet}!{coordinate} 公式结果无法校验：{exc}。请在 Excel 中核对并粘贴为值后回传。',
                                422, 'FORMULA_RESULT_UNVERIFIED') from exc


class NumericExpression:
    PRECEDENCE = {'+': 10, '-': 10, '*': 20, '/': 20, '^': 30}

    def __init__(self, context, sheet, formula):
        if len(formula) > 8192:
            raise ValueError('公式过长')
        self.ctx, self.sheet = context, sheet
        try:
            self.tokens = [t for t in Tokenizer(formula).items if t.type != 'WHITE-SPACE']
        except Exception as exc:
            raise ValueError('公式格式无法识别') from exc
        if len(self.tokens) > 2000:
            raise ValueError('公式过于复杂')
        self.index = 0

    def peek(self):
        return self.tokens[self.index] if self.index < len(self.tokens) else None

    def take(self):
        token = self.peek()
        if token is None:
            raise ValueError('公式不完整')
        self.index += 1
        return token

    def parse(self):
        result = self.expression()
        if self.peek() is not None or isinstance(result, list):
            raise ValueError('不支持此公式操作')
        return result

    def expression(self, minimum=0):
        token = self.take()
        if token.type == 'OPERATOR-PREFIX' and token.value in {'+', '-'}:
            # Excel negation precedes exponentiation; equal-precedence binary
            # operators are evaluated left to right (Microsoft Support operator table).
            left = self.ctx.number(self.expression(40)) * (-1 if token.value == '-' else 1)
        elif token.type == 'PAREN' and token.subtype == 'OPEN':
            left = self.expression()
            closing = self.take()
            if closing.type != 'PAREN' or closing.subtype != 'CLOSE':
                raise ValueError('括号不匹配')
        elif token.type == 'OPERAND':
            if token.subtype == 'NUMBER':
                left = self.ctx.number(token.value)
            elif token.subtype == 'RANGE':
                left = self.reference(token.value)
            elif token.subtype == 'LOGICAL':
                left = token.value.upper() == 'TRUE'
            else:
                raise ValueError('仅支持数值公式、单元格引用和数值函数')
        elif token.type == 'FUNC' and token.subtype == 'OPEN':
            left = self.function(token.value[:-1].upper())
        else:
            raise ValueError('不支持此公式操作')
        while (next_token := self.peek()) is not None:
            if next_token.type == 'OPERATOR-POSTFIX' and next_token.value == '%':
                self.take(); left = self.ctx.number(left) / 100
                continue
            op = next_token.value
            if next_token.type != 'OPERATOR-INFIX' or op not in self.PRECEDENCE or self.PRECEDENCE[op] < minimum:
                break
            self.take()
            right = self.expression(self.PRECEDENCE[op] + 1)
            a, b = self.ctx.number(left), self.ctx.number(right)
            if op == '+': left = a + b
            elif op == '-': left = a - b
            elif op == '*': left = a * b
            elif op == '/': left = a / b
            else:
                if b != int(b) or abs(b) > 100:
                    raise ValueError('仅支持绝对值不超过 100 的整数指数')
                left = a ** int(b)
        return left

    def reference(self, text):
        if '[' in text or ']' in text:
            raise ValueError('外部工作簿引用不能自动校验')
        sheet, address = text.rsplit('!', 1) if '!' in text else (self.sheet, text)
        if sheet.startswith("'") and sheet.endswith("'"):
            sheet = sheet[1:-1].replace("''", "'")
        if sheet not in self.ctx.book.sheetnames:
            raise ValueError('公式引用的工作表不存在')
        if not re.fullmatch(r'\$?[A-Za-z]{1,3}\$?[1-9]\d{0,6}(?::\$?[A-Za-z]{1,3}\$?[1-9]\d{0,6})?', address):
            raise ValueError('仅支持有明确边界的单元格区域引用')
        left, top, right, bottom = range_boundaries(address)
        if bottom > 20100 or right > 10201 or (bottom-top+1)*(right-left+1) > 20000:
            raise ValueError('公式引用区域超出当前容量')
        cells = [self.ctx.cell(sheet, cell.coordinate) for row in self.ctx.book[sheet].iter_rows(min_row=top, max_row=bottom, min_col=left, max_col=right) for cell in row]
        return cells if ':' in address else cells[0]

    def function(self, name):
        if name not in {'SUM', 'MIN', 'MAX', 'AVERAGE', 'COUNT', 'ROUND', 'ABS'}:
            raise ValueError(f'函数 {name} 暂不支持独立校验')
        args = []
        token = self.peek()
        if token is not None and not (token.type == 'FUNC' and token.subtype == 'CLOSE'):
            while True:
                args.append(self.expression())
                token = self.peek()
                if token is not None and token.type == 'SEP' and token.subtype == 'ARG':
                    self.take(); continue
                break
        close = self.take()
        if close.type != 'FUNC' or close.subtype != 'CLOSE':
            raise ValueError('函数参数格式无效')
        values = [v for arg in args for v in (arg if isinstance(arg, list) else [arg])]
        if name in {'ABS', 'ROUND'}:
            if len(values) != (1 if name == 'ABS' else 2):
                raise ValueError('函数参数数量不符')
            if name == 'ABS': return abs(self.ctx.number(values[0]))
            places = self.ctx.number(values[1])
            if places != int(places) or abs(places) > 100:
                raise ValueError('ROUND 精度超出支持范围')
            return self.ctx.number(values[0]).quantize(Decimal(10) ** -int(places), rounding=ROUND_HALF_UP)
        if any(isinstance(arg, bool) for arg in args):
            raise ValueError('此数值函数的逻辑值参数需要在 Excel 中核对')
        numeric = [self.ctx.number(v) for v in values if isinstance(v, (int, float, Decimal)) and not isinstance(v, bool)]
        if name == 'SUM': return sum(numeric, Decimal(0))
        if name == 'COUNT': return Decimal(len(numeric))
        if name == 'MIN': return min(numeric, default=Decimal(0))
        if name == 'MAX': return max(numeric, default=Decimal(0))
        if not numeric: raise ValueError('AVERAGE 没有可计算的数值')
        return sum(numeric, Decimal(0)) / len(numeric)
