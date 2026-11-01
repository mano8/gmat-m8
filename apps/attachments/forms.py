"""Forms of the attachment screens. The upload itself is read by the upload handler."""

from django import forms
from django.utils.translation import gettext_lazy as _


class RemoveForm(forms.Form):
    """Why the attachment is removed: required of a moderator, optional on one's own draft
    (the service decides which applies)."""

    reason = forms.CharField(
        label=_("Reason"), required=False, max_length=500, widget=forms.Textarea(attrs={"rows": 2})
    )
