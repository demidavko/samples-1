from __future__ import unicode_literals

import random
import string
from urllib.parse import urljoin

from django.contrib.sites.models import Site
from django.core.urlresolvers import reverse
from django.core.validators import MinValueValidator, MaxValueValidator
from django.db import models
from django.db.models import Sum
from django_extensions.db.fields.json import JSONField
from payments import PurchasedItem
from payments.models import BasePayment

from applications.base.models import TimeStampedModel


class PaymentMethod(TimeStampedModel):
    name = models.CharField(help_text="Human readable name of this "
                                      "payment method",
                            max_length=255)
    variant = models.CharField(help_text="Should be based on the "
                                         "settings.PAYMENT_VARIANTS",
                               max_length=255)

    def __str__(self):
        return self.name


class Order(TimeStampedModel):
    # Service types
    SOCIAL_PROFILES = 0
    REPUTATION_CASE = 1

    SERVICE_TYPE_CHOICES = (
        (SOCIAL_PROFILES, "Social Profiles"),
        (REPUTATION_CASE, "Reputation Case")
    )

    user = models.ForeignKey("profiles.User")
    service_type = models.IntegerField(default=SOCIAL_PROFILES,
                                       choices=SERVICE_TYPE_CHOICES)
    items = JSONField(blank=True, default=[],
                      help_text="IDs of SocialProfiles or ReputationCases")
    payment_method = models.ForeignKey("accounting.PaymentMethod")
    coupon_code = models.ForeignKey("accounting.CouponCode",
                                    blank=True, null=True)

    class Meta:
        permissions = (
            ("view_order", "View Order"),
        )

    def order_ready(self):
        if self.service_type == self.SOCIAL_PROFILES:
            items = self.get_items()

            for item in items:
                if item.status not in (item.CREATED, item.FAILED):
                    return False

            return True
        else:
            return False

    @property
    def order_progress(self):
        """
        Returns the current progress of the order in the following format:
        {
            “created”: 5,
            “total”: 20,
            “progress”: “25%”
        }
        """
        if self.service_type == self.SOCIAL_PROFILES:
            from applications.social_sites.models import SocialProfile
            items = self.get_items()
            total_count = len(items)
            created_count = (items
                             .filter(status__in=(SocialProfile.CREATED,
                                                 SocialProfile.FAILED))
                             .count())
            progress = round(created_count / total_count, 2) * 100
            progress = int(progress)
            return {
                "created": created_count,
                "total": total_count,
                "progress": "{}%".format(progress)
            }

    def get_items(self):
        """
        Returns QuerySet of items depending on the order type
        """
        if self.service_type == self.SOCIAL_PROFILES:
            from applications.social_sites.models import SocialProfile as Model
        else:
            from applications.reputation.models import ReputationCase as Model

        return Model.objects.filter(pk__in=self.items)

    def add_items(self, items, commit=True):
        """
        Adds items to the order
        """
        self.items.extend([item.pk for item in items])

        if commit:
            self.save()

    def add_item(self, item, commit=True):
        """
        Adds one item to the order
        """
        self.add_items([item], commit=commit)

    def create_payment(self, amount=None):
        """
        Creates a payment instance for that order with provided amount,
        if no amount passed, we calculate it depending on the items
        that belongs to this order
        """
        total = float(amount) if amount else self.calculate_total(False)
        if self.discount:
            total *= 1 - self.discount / 100.
        payment = Payment.objects.create(variant=self.payment_method.variant,
                                         order=self, total=total,
                                         currency="USD")
        return payment

    def calculate_total(self, include_discount=True):
        """
        Calculates total amount for this order
        :param include_discount: Makes total amount discount aware
        """
        items = self.get_items()
        if self.service_type == self.SOCIAL_PROFILES:
            # item is a SocialProfile instance
            sum_expr = Sum("site__price")
        else:
            # item is a ReputationCase instance
            sum_expr = Sum("price")

        total = items.aggregate(total=sum_expr).get("total") or 0.0
        total = float(total)

        do_calc_discount = self.discount and include_discount
        total *= 1 - self.discount / 100. if do_calc_discount else 1
        return round(total, 2)

    @property
    def discount(self):
        """
        Discount amount getter
        """
        return self.coupon_code.discount if self.coupon_code else 0

    @property
    def has_payment(self):
        return Payment.objects.filter(order=self).exists()

    @property
    def payment_status(self):
        """
        Mirrors payment status that associated with this order
        """
        try:
            return self.payment.get_status_display()
        except Exception:
            return "Payment Pending"

    @property
    def total(self):
        """
        String representation of total amount of this order.
        :return: e.g. "$30"
        """
        return "${}".format(self.calculate_total())

    def get_absolute_url(self):
        return reverse("accounting:order-overview", kwargs={"pk": self.pk})

    def __str__(self):
        return "Order #{} ({})".format(self.pk, self.payment_status)


class Payment(BasePayment):
    order = models.OneToOneField("accounting.Order", related_name="payment",
                                 db_index=True)

    class Meta:
        permissions = (
            ("view_payment", "View Payment"),
        )

    def get_success_url(self):
        url = reverse("accounting:payment-success",
                      kwargs={"payment_id": self.pk,
                              "payment_token": self.token})
        return self.get_url(url)

    def get_failure_url(self):
        url = reverse("accounting:payment-failure",
                      kwargs={"payment_id": self.pk,
                              "payment_token": self.token})
        return self.get_url(url)

    def get_purchased_items(self):
        has_items = bool(self.order.items)
        if has_items:
            name = "Order #{}".format(self.order_id)
            service_type = (self.order.get_service_type_display()
                            .lower()
                            .replace(" ", "-"))
            sku = "order-{}-{}".format(service_type, self.order_id)
            currency = self.currency
            amount = self.total
            yield PurchasedItem(
                name=name, quantity=1, sku=sku,
                price=amount, currency=currency,
            )

    def save(self, **kwargs):
        self.variant = self.order.payment_method.variant
        return super(Payment, self).save(**kwargs)

    def get_absolute_url(self):
        return reverse("accounting:payment-overview",
                       kwargs={"payment_id": self.pk,
                               "payment_token": self.token})

    @staticmethod
    def get_url(url):
        """
        Returns protocol, domain and url joined
        """
        from django.conf import settings

        is_ssl = settings.SECURE_SSL_REDIRECT
        scheme = "https" if is_ssl else "http"
        netloc = Site.objects.get_current().domain

        base = "{scheme}://{netloc}".format(scheme=scheme, netloc=netloc)
        return urljoin(base, url)

    def __str__(self):
        return "{} - {} ({})".format(self.order.user,
                                     self.order.get_service_type_display(),
                                     self.status)


class CouponCode(TimeStampedModel):
    sales_rep = models.ForeignKey("profiles.User",
                                  verbose_name="Sales Representative",
                                  limit_choices_to={"is_sales_rep": True})
    code = models.CharField(max_length=255, help_text="Leaving this field "
                                                      "empty will generate "
                                                      "a random coupon code",
                            unique=True, blank=True)
    discount = models.FloatField(verbose_name="Discount Percentage", default=0,
                                 validators=[MinValueValidator(0.0),
                                             MaxValueValidator(100.0)])
    commission = models.FloatField(verbose_name="Commission Percentage",
                                   default=0,
                                   validators=[MinValueValidator(0.0),
                                               MaxValueValidator(100)])

    class Meta:
        permissions = (
            ("view_coupon_code", "View Coupon Code"),
        )

    def save(self, *args, **kwargs):
        if not self.code:
            existing_codes = (self._default_manager
                              .values_list("code", flat=True))
            while True:
                code = CouponCode.generate_code()
                if code not in existing_codes:
                    self.code = code
                    break

        return super(CouponCode, self).save(*args, **kwargs)

    @classmethod
    def generate_code(cls, length=10, include_numbers=False,
                      all_uppercase=True):
        base_str = string.ascii_uppercase
        base_str += string.ascii_lowercase if not all_uppercase else ""
        base_str += "1234567890" if include_numbers else ""
        return "".join([random.choice(base_str) for _ in range(length)])

    def __str__(self):
        return ("{} ({}), discount: {}%, commission {}%"
                .format(self.sales_rep, self.code,
                        self.discount, self.commission))


from . import signals
