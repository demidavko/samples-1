import json

import pytest
from django.core.urlresolvers import reverse
from mixer.backend.django import mixer

from applications.accounting.models import PaymentMethod, Order, CouponCode, \
    Payment
from applications.profiles.models import User
from applications.reputation.models import ReputationCase
from applications.social_sites.models import Site, GMailAccount, SocialProfile
from applications.profiles.tests.fixtures import test_user
from applications.social_sites.utils import get_random_weight

pytestmark = pytest.mark.django_db


@pytest.fixture
def site():
    site_obj = mixer.blend(Site, price=10, weight=get_random_weight())
    return site_obj


@pytest.fixture
def payment_method():
    return mixer.blend(PaymentMethod, variant="default")


""" Cart Adding / Removing / Access Tests """


def test_cart_valid_authenticated(client, site, test_user):
    logged_in = client.login(username=test_user.username,
                             password=test_user.username)
    assert logged_in is True, "Client did not login"
    social_profile = mixer.blend(SocialProfile, site=site, username="test",
                                 password="test")
    url = reverse("accounting:cart-add")
    response = client.get(url + "?site={}".format(social_profile.pk))

    assert response.status_code == 200, "Response code should be 200"
    resp_json = json.loads(response.content.decode("utf-8"))
    total = float(resp_json.get("total") or 0)
    site_price = float(site.price.amount)
    expected_message = "{} have been successfully added to the cart.".format(site.name)
    assert total == site_price, "Response should contain 'total' value"
    assert resp_json.get("message") == expected_message, "Response should contain success message"

    url = reverse("accounting:cart-remove")
    response = client.get(url + "?site={}".format(social_profile.pk))

    resp_json = json.loads(response.content.decode("utf-8"))
    total = float(resp_json.get("total") or 0)
    expected_message = "{} have been successfully removed from the cart.".format(site.name)
    assert total == 0.0, "Response should contain 'total' value"
    assert resp_json.get("message") == expected_message, "Response should contain success message"


def test_cart_invalid_authenticated(client, site, test_user):
    logged_in = client.login(username=test_user.username,
                             password=test_user.username)
    assert logged_in is True, "Client did not login"

    response = client.get(reverse("accounting:cart-add"))

    assert response.status_code == 400, "Response code should be 400, because ?site arg is not present"
    assert b"No Site ID provided" in response.content, "Error message is not present in response body"

    response = client.get(reverse("accounting:cart-remove"))

    assert response.status_code == 400, "Response code should be 400, because ?site arg is not provided"
    assert b"No Site ID provided" in response.content, "Error message is not present in response"


def test_cart_valid_unauthenticated(client, site):
    social_profile = mixer.blend(SocialProfile, site=site, username="test",
                                 password="test")
    url = reverse("accounting:cart-add")
    response = client.get(url + "?site={}".format(social_profile.pk))

    assert response.status_code == 200, "Response code should be 200"
    resp_json = json.loads(response.content.decode("utf-8"))
    total = float(resp_json.get("total") or 0)
    site_price = float(site.price.amount)
    expected_message = "{} have been successfully added to the cart.".format(site.name)
    assert total == site_price, "Response should contain 'total' value"
    assert resp_json.get("message") == expected_message, "Response should contain success message"

    url = reverse("accounting:cart-remove")
    response = client.get(url + "?site={}".format(social_profile.pk))

    resp_json = json.loads(response.content.decode("utf-8"))
    total = float(resp_json.get("total") or 0)
    expected_message = "{} have been successfully removed from the cart.".format(site.name)
    assert total == 0.0, "Response should contain 'total' value"
    assert resp_json.get("message") == expected_message, "Response should contain success message"


def test_cart_detail_view_get_authenticated(client, test_user):
    logged_in = client.login(username=test_user.username,
                             password=test_user.username)
    assert logged_in is True, "User did not login"

    response = client.get(reverse("accounting:cart-overview"))
    assert response.status_code == 200, "User should be able to access shopping cart page"


def test_cart_detail_view_get_unauthenticated(client):
    response = client.get(reverse("accounting:cart-overview"))
    assert response.status_code == 200, "User should be able to see cart overview page"


def test_cart_detail_view_post_authenticated(client, test_user, payment_method):
    logged_in = client.login(username=test_user.username,
                             password=test_user.username)
    assert logged_in is True, "User did not login"

    mixer.blend(GMailAccount)
    data = {"payment_method": payment_method.pk}
    response = client.post(reverse("accounting:cart-overview"), data)
    assert response.status_code == 302, "User should be redirected to the payment view"


def test_payment_details_get_authenticated_valid_user(client, test_user,
                                                      payment_method):
    logged_in = client.login(username=test_user.username,
                             password=test_user.username)
    assert logged_in is True, "User did not login"

    order = mixer.blend(Order, user=test_user, payment_method=payment_method)
    payment = mixer.blend(Payment, order=order, variant="default")

    response = client.get(payment.get_absolute_url())
    assert response.status_code == 200, "User should be able to access checkout page"


def test_payment_details_get_authenticated_invalid_user(client, test_user,
                                                        payment_method):
    logged_in = client.login(username=test_user.username,
                             password=test_user.username)
    assert logged_in is True, "User did not login"

    user = mixer.blend(User)
    order = mixer.blend(Order, user=user, payment_method=payment_method)
    payment = order.create_payment()

    response = client.get(payment.get_absolute_url())
    assert response.status_code == 404, "Non owner of the payment should not be able to access payment detail view"


def test_payment_details_get_unauthenticated(client):
    payment = mixer.blend(Payment, variant="default")
    response = client.get(payment.get_absolute_url())
    assert response.status_code == 404, "Unauthenticated users should not be able to access payment detail view"


def test_payment_checkout_get_authenticated_valid_user(client, test_user,
                                                       payment_method):
    logged_in = client.login(username=test_user.username,
                             password=test_user.username)
    assert logged_in is True, "User did not login"

    order = mixer.blend(Order, user=test_user, payment_method=payment_method)
    payment = order.create_payment()

    response = client.get(payment.get_absolute_url(), follow=True)
    assert response.status_code == 200, "User should be able to access checkout page"


def test_payment_checkout_post_authenticated_valid_user(client, test_user,
                                                        payment_method):
    logged_in = client.login(username=test_user.username,
                             password=test_user.username)
    assert logged_in is True, "User did not login"

    order = mixer.blend(Order, user=test_user, payment_method=payment_method)
    payment = order.create_payment()
    data = {
        "status": "confirmed",
        "verification_result": "confirmed",
        "fraud_status": "unknown",
        "gateway_response": "3ds-redirect",
    }
    response = client.post(payment.get_absolute_url(), data=data)
    assert response.status_code == 302, "User should be redirected to the payment success page"


def test_payment_checkout_post_authenticated_invalid_user(client, test_user,
                                                          payment_method):
    logged_in = client.login(username=test_user.username,
                             password=test_user.username)
    assert logged_in is True, "User did not login"

    user = mixer.blend(User)
    order = mixer.blend(Order, user=user, payment_method=payment_method)
    payment = order.create_payment()
    data = {
        "status": "confirmed",
        "verification_result": "confirmed",
        "fraud_status": "unknown",
        "gateway_response": "3ds-redirect",
    }
    response = client.post(payment.get_absolute_url(), data=data)
    assert response.status_code == 404, "Non owner of the payment should not be able to post to the checkout view"


def test_order_detail_get_authenticated(client, test_user):
    logged_in = client.login(username=test_user.username,
                             password=test_user.username)
    assert logged_in is True, "User did not login"

    order = mixer.blend(Order, user=test_user)
    response = client.get(reverse("accounting:order-overview",
                                  kwargs={"pk": order.pk}))
    assert response.status_code == 200, "User should be able to reach order detail view"


def test_order_detail_post_authenticated(client, test_user):
    logged_in = client.login(username=test_user.username,
                             password=test_user.username)
    assert logged_in is True, "User did not login"

    payment_method = mixer.blend(PaymentMethod)
    case = mixer.blend(ReputationCase, user=test_user, price=1000)
    order = mixer.blend(Order, user=test_user, service_type=Order.REPUTATION_CASE, items=[case.pk])

    data = {"payment_method": payment_method.pk, "coupon_code": ""}
    response = client.post(reverse("accounting:order-overview",
                                   kwargs={"pk": order.pk}), data)

    assert response.status_code == 302, "User should be redirected to the payment page"


def test_order_detail_get_unauthenticated(client):
    order = mixer.blend(Order)
    response = client.get(reverse("accounting:order-overview",
                                  kwargs={"pk": order.pk}))
    assert response.status_code == 302, "User should be redirected to the login page"


def test_order_detail_get_authenticated_invalid_perms(client, test_user):
    logged_in = client.login(username=test_user.username,
                             password=test_user.username)
    assert logged_in is True, "User did not login"

    order = mixer.blend(Order)
    response = client.get(reverse("accounting:order-overview",
                                  kwargs={"pk": order.pk}))
    assert response.status_code == 404, "Random user should not be able to access the detail order view"


def test_order_list_get_authenticated(client, test_user):
    logged_in = client.login(username=test_user.username,
                             password=test_user.username)
    assert logged_in is True, "User did not login"

    mixer.cycle(5).blend(Order, user=test_user)
    mixer.blend(Order, user__username="test")
    response = client.get(reverse("accounting:order-list"))

    assert response.status_code == 200, "User should be able to reach order list view"
    assert len(response.context["orders"]) == 5, "User should see only 5 orders that belongs to him"


def test_order_list_get_unauthenticated(client):
    response = client.get(reverse("accounting:order-list"))
    assert response.status_code == 302, "User should be redirected to the login page"


class TestCouponCodeListView(object):
    def test_coupon_report_get_authenticated_sales_rep(self, client, test_user):
        cred = "sales_rep"
        sales_rep = mixer.blend(User, is_sales_rep=True, username=cred)
        sales_rep.set_password(cred)
        sales_rep.save()

        test_user.is_sales_rep = True
        test_user.save()

        mixer.cycle(5).blend(CouponCode, sales_rep=sales_rep)
        mixer.cycle(5).blend(CouponCode, sales_rep=test_user)

        logged_in = client.login(username=cred, password=cred)
        assert logged_in is True, "User did not login"

        response = client.get(reverse("accounting:coupon-list"))
        assert response.status_code == 200, "Sales Rep should be able to access coupon list view"
        assert len(response.context["coupons"]) == 5, "Sales Rep should see only 5 coupon codes that belongs to him"

    def test_coupon_report_get_authenticated_non_sales_rep(self, client, test_user):
        logged_in = client.login(username=test_user.username,
                                 password=test_user.username)
        assert logged_in is True, "User did not login"

        response = client.get(reverse("accounting:coupon-list"))
        assert response.status_code == 404, "Non sales rep should not be able to access coupon list view"

    def test_coupon_report_get_authenticated_staff_user(self, client):
        cred = "staff_user"
        staff_user = mixer.blend(User, is_staff=True,
                                 is_rep_manager=True,
                                 username=cred)
        staff_user.set_password(cred)
        staff_user.save()

        # Blending two times so each time new
        # sales_rep is generated for each 5 coupons
        mixer.cycle(5).blend(CouponCode)
        mixer.cycle(5).blend(CouponCode)

        logged_in = client.login(username=cred, password=cred)
        assert logged_in is True, "User did not login"

        response = client.get(reverse("accounting:coupon-list"))
        assert response.status_code == 200, "Staff member should be able to access coupon list view"
        assert len(response.context["coupons"]) == 10, "Staff member should see all the coupon codes"

    def test_coupon_report_get_unauthenticated(self, client):
        response = client.get(reverse("accounting:coupon-list"))
        assert response.status_code == 404, "Unauthenticated users should not be able to access coupon list view"


class TestAccountingReport(object):
    def test_get_staff(self, client, test_user):
        test_user.is_staff = True
        test_user.save()

        logged_in = client.login(username=test_user.username,
                                 password=test_user.username)
        assert logged_in is True, "User did not login"

        response = client.get(reverse("accounting:accounting-report"))
        assert response.status_code == 200, "Staff user should be able to review accounting report page"
        assert bool(response.context["table"]), "Table should be available in the context"

    def test_get_non_staff(self, client, test_user):
        logged_in = client.login(username=test_user.username,
                                 password=test_user.username)
        assert logged_in is True, "User did not login"

        response = client.get(reverse("accounting:accounting-report"))
        assert response.status_code == 404, "Non staff user shouldn't be able to review accounting report page"

    def test_get_unauthenticated(self, client):
        response = client.get(reverse("accounting:accounting-report"))
        assert response.status_code == 404, "Unauthenticated users should not be able to review accounting report page"
