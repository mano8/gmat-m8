"""``{% can %}``: show a piece of UI only to users holding a capability (plan §3.6).

    {% load access %}
    {% can "price.view" %}…{% endcan %}
    {% can "request.review" material_request %}…{% else %}…{% endcan %}

It hides UI only and is never the security control: views check with ``require`` and the
selectors' ``visible_to``, services with ``authorize_in_txn``.
"""

from typing import Any

from django import template
from django.db.models import Model
from django.template.base import FilterExpression, NodeList, Parser, Token

from apps.access.middleware import access_for

register = template.Library()


class CanNode(template.Node):
    def __init__(
        self,
        capability: FilterExpression,
        obj: FilterExpression | None,
        if_granted: NodeList,
        otherwise: NodeList,
    ) -> None:
        self.capability = capability
        self.obj = obj
        self.if_granted = if_granted
        self.otherwise = otherwise

    def granted(self, context: template.Context) -> bool:
        request = context.get("request")
        if request is None:
            return False
        ctx = access_for(request)
        capability = str(self.capability.resolve(context))
        if self.obj is None:
            return ctx.has(capability)
        obj = self.obj.resolve(context)
        return isinstance(obj, Model) and ctx.can(capability, obj)

    def render(self, context: template.Context) -> Any:
        nodes = self.if_granted if self.granted(context) else self.otherwise
        return nodes.render(context)


@register.tag("can")
def do_can(parser: Parser, token: Token) -> CanNode:
    bits = token.split_contents()
    if len(bits) not in {2, 3}:
        raise template.TemplateSyntaxError(
            f'Usage: {{% {bits[0]} "capability" [object] %}}…{{% end{bits[0]} %}}'
        )
    capability = parser.compile_filter(bits[1])
    obj = parser.compile_filter(bits[2]) if len(bits) == 3 else None
    if_granted = parser.parse(("else", "endcan"))
    otherwise = NodeList()
    if parser.next_token().contents == "else":
        otherwise = parser.parse(("endcan",))
        parser.delete_first_token()
    return CanNode(capability, obj, if_granted, otherwise)
