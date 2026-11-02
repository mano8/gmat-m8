"""The CSV exports (plan M7.3, ``export.csv``): the page that lists them and the downloads.

Both only read, through ``apps.exports.selectors``. A download streams the rows as UTF-8 with a
byte-order mark (so Excel reads accents) and is an attachment, never shown in the browser.
"""

import csv
from collections.abc import Iterator
from typing import Any

from django.http import Http404, HttpRequest, HttpResponse, StreamingHttpResponse
from django.http.response import HttpResponseBase
from django.shortcuts import render
from django.utils import timezone
from django.views.decorators.http import require_safe

from apps.access.middleware import access_for
from apps.access.mixins import require_capability
from apps.exports import selectors


class _Echo:
    def write(self, value: str) -> str:
        return value


def _lines(header: list[str], rows: Iterator[list[Any]]) -> Iterator[str]:
    writer = csv.writer(_Echo(), lineterminator="\r\n")
    yield "\ufeff" + writer.writerow(header)
    for row in rows:
        yield writer.writerow(row)


@require_safe
@require_capability(selectors.EXPORT)
def index(request: HttpRequest) -> HttpResponse:
    return render(request, "exports/index.html", {"exports": selectors.EXPORTS})


@require_safe
@require_capability(selectors.EXPORT)
def download(request: HttpRequest, key: str) -> HttpResponseBase:
    export = selectors.BY_KEY.get(key)
    if export is None:
        raise Http404
    header, rows = export.build(access_for(request))
    response = StreamingHttpResponse(_lines(header, rows), content_type="text/csv; charset=utf-8")
    stamp = timezone.now().strftime("%Y%m%d")
    response["Content-Disposition"] = f'attachment; filename="gmat-m8-{key}-{stamp}.csv"'
    return response
