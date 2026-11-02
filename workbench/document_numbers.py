"""Decimal comparisons at the precision actually printed in a document."""
from decimal import Decimal,InvalidOperation,ROUND_HALF_UP,localcontext


def compare(claim,expected):
    if claim['kind']=='hash':
        return any(v.startswith(claim['value']) if claim.get('prefix') else v==claim['value'] for v in expected),'exact hash/prefix'
    exact=claim['kind'].startswith('tests')
    precision=claim.get('precision',0);scale=Decimal(claim.get('scale','1'))
    method='exact numeric count' if exact else f'Decimal ROUND_HALF_UP to document {precision} decimal places; unit scale {scale}'
    try:
        target=Decimal(claim.get('raw',claim['value']))
        if not target.is_finite():return False,method
        with localcontext() as context:
            context.prec=100
            quantum=Decimal(1).scaleb(-precision)
            return any(v==target if exact else (v/scale).quantize(quantum,rounding=ROUND_HALF_UP)==target for v in expected if v.is_finite()),method
    except (InvalidOperation,ValueError,ZeroDivisionError):return False,method
