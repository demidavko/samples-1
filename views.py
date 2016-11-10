import json
from functools import wraps

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.contrib.auth.mixins import LoginRequiredMixin
from django.db.models import Q
from django.http.response import HttpResponseBadRequest, Http404, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render

from carton.cart import Cart
from django.views.generic import DetailView, ListView
from django_tables2 import SingleTableView
from guardian.shortcuts import get_perms, get_objects_for_user
from payments import RedirectNeeded

from applications.accounting.forms import OrderForm, AccountingReportForm
from applications.accounting.models import Payment, Order, CouponCode
from applications.accounting.tables import PaymentTable
from applications.base.mixins import ObjectPermissionRequiredMixin, \
    ProtectedViewMixin
from applications.base.utils import PYJSONEncoder
from applications.social_sites.forms import SocialProfileForm
from applications.social_sites.tasks import run_worker
from applications.social_sites.models import SocialProfile, OrderSite, \
    GMailAccount


def site_id_required(view):
    """
    Checks if client provided a Site ID as a GET parameter,
    if not, returns 400.
    """

    @wraps(view)
    def wrapper(request):
        site_id = request.GET.get("site")
        if not site_id:
            return HttpResponseBadRequest("No Site ID provided.")
        return view(request, site_id)

    return wrapper


@site_id_required
def add_to_cart(request, site_id):
    """
    Adds Site to the cart
    """
    cart = Cart(request.session)
    order_site = get_object_or_404(OrderSite, pk=site_id)
    cart.add(product=order_site, price=order_site.site.price.amount)

    message = ("{} have been successfully added to the cart."
               .format(order_site.site.name))
    return JsonResponse({"total": cart.total, "message": message})


@site_id_required
def remove_from_cart(request, site_id):
    """
    Removes Site from the cart
    """
    cart = Cart(request.session)
    ordered_site = get_object_or_404(OrderSite, pk=site_id)
    cart.remove_single(product=ordered_site)
    message = ("{} have been successfully removed from the cart."
               .format(ordered_site.site.name))
    return JsonResponse({"message": message, "total": cart.total})


def shopping_cart_overview(request):
    """
    Provides an ability for users to overview their cart items
    choose payment method, input data for their future profiles
    and move to the checkout page
    """
    if request.method == "POST":
        return redirect("accounting:cart-signup-details")
    return render(request, "html/cart-overview.html")


def shopping_cart_signup_details(request):
    """
    View where user can input details about the accounts he is going to
    register through profileyou
    """
    social_profile_form = SocialProfileForm(request.POST or None,
                                            files=request.FILES or None)

    if request.POST and social_profile_form.is_valid():
        profile_data = social_profile_form.cleaned_data
        serialized_profile_data = json.dumps(profile_data,
                                             cls=PYJSONEncoder)
        request.session["PROFILE_DATA"] = serialized_profile_data

        return redirect("accounting:cart-checkout")
    return render(request, "html/cart-signup-details.html",
                  {"form": social_profile_form})


@login_required
def cart_checkout(request):
    """
    Here user chooses his payment method and
    gets redirected to the payment page
    """
    order_form = OrderForm(request.POST or None)

    if request.POST and order_form.is_valid():
        # Checkout process is fired up so we need to block another
        # potential checkout, we keep that info in the session.
        # And reset the checkout info when payment is done
        checkout_info = request.session.get("CHECKOUT_INFO")
        if checkout_info:
            is_blocked = checkout_info.get("is_blocked", False)
        else:
            is_blocked = False

        if is_blocked:
            payment_url = checkout_info.get("payment_url")
            return redirect(payment_url)

        cart = Cart(request.session)

        profile_data = json.loads(request.session["PROFILE_DATA"])

        order = order_form.save(commit=False)
        order.user = request.user

        # At this point user is already registered
        # And by having his cart we know who is performed search
        order_sites = cart.products
        if order_sites:
            user_search = order_sites[0].user_search
            if not user_search.user:
                user_search.user = request.user
                user_search.save()

        if settings.DEBUG:
            from mixer.backend.django import mixer
            email = mixer.blend(GMailAccount)
        else:
            email = (GMailAccount.objects
                     .filter(user__isnull=True,
                             email__endswith="pokemail.net")
                     .first())
            email.user = request.user
            email.save()

        unpaid = SocialProfile.REQUESTED_CREATION_UNPAID
        for order_site in order_sites:
            username = order_site.available_username
            profile = SocialProfile.objects.create(user=request.user,
                                                   status=unpaid,
                                                   site=order_site.site,
                                                   username=username,
                                                   email=email,
                                                   **profile_data)
            order.add_item(profile, commit=False)
        order.save()
        payment = order.create_payment(cart.total)
        payment_url = payment.get_absolute_url()

        # Set the checkout info
        request.session["CHECKOUT_INFO"] = {
            "is_blocked": True,
            "payment_url": payment_url,
        }
        request.session.modified = True

        return redirect(payment.get_absolute_url())

    return render(request, "html/checkout.html",
                  {"form": order_form})


def payment_details(request, payment_id, payment_token):
    payment = get_object_or_404(Payment, pk=payment_id, token=payment_token)
    if "view_order" not in get_perms(request.user, payment.order):
        raise Http404

    try:
        form = payment.get_form(data=request.POST or None)
    except RedirectNeeded as redirect_to:
        return redirect(str(redirect_to))

    return render(request, "html/payment.html",
                  {"form": form, "payment": payment})


@login_required
def payment_success(request, payment_id, payment_token):
    payment = get_object_or_404(Payment, pk=payment_id, token=payment_token,
                                order__user=request.user)
    payment.capture()

    confirmed = payment.status == "confirmed"
    if payment.order.service_type == Order.SOCIAL_PROFILES:
        # User done his payment. So we need to clean up his cart
        Cart(request.session).clear()

        # And session's checkout info
        request.session["CHECKOUT_INFO"] = {}
        request.session.modified = True

    if payment.order.service_type == Order.SOCIAL_PROFILES and confirmed:
        unpaid = SocialProfile.REQUESTED_CREATION_UNPAID
        social_profile_ids = list(payment.order
                                  .get_items()
                                  .filter(status=unpaid)
                                  .values_list("pk", flat=True))

        (SocialProfile.objects
         .filter(pk__in=social_profile_ids)
         .update(status=SocialProfile.REQUESTED_CREATION_PAID))

        for pk in social_profile_ids:
            run_worker.apply_async(args=(pk,))

        # Update profile status 
        if not request.user.progress.made_an_order:
            request.user.progress.made_an_order = True
            request.user.progress.save()

        level = messages.INFO
        message = "Payment have been successfully processed!"

    elif payment.order.service_type == Order.REPUTATION_CASE and confirmed:
        from applications.reputation.models import ReputationCase
        unpaid = ReputationCase.OPEN_UNPAID
        paid = ReputationCase.OPEN_PAID
        payment.order.get_items().filter(status=unpaid).update(status=paid)

        level = messages.INFO
        message = "Payment have been successfully processed!"

    else:
        level = messages.WARNING
        message = "Payment failed!"

    messages.add_message(request, level, message)
    return redirect(payment.order.get_absolute_url())


@login_required
def payment_failure(request, payment_id, payment_token):
    payment = get_object_or_404(Payment, pk=payment_id, token=payment_token,
                                order__user=request.user)
    messages.add_message(request, messages.WARNING, "Payment failed!")

    request.session["CHECKOUT_INFO"] = {}
    request.session.modified = True

    return redirect(payment.order.get_absolute_url())


class OrderDetailView(LoginRequiredMixin,
                      ObjectPermissionRequiredMixin,
                      DetailView):
    perms = ("view_order",)
    model = Order
    template_name = "html/order-overview.html"

    def get_context_data(self, **kwargs):
        order = kwargs.get("object")
        form = OrderForm(self.request.POST or None, instance=order)
        kwargs.update(form=form)
        return super(OrderDetailView, self).get_context_data(**kwargs)

    def post(self, request, *args, **kwargs):
        self.object = self.get_object()
        context = self.get_context_data(object=self.object)

        form = context.get("form")

        if not self.object.has_payment and form.is_valid():
            self.object = form.save()
            payment = self.object.create_payment()
            return redirect(payment.get_absolute_url())

        return self.render_to_response(context)


class OrderListView(LoginRequiredMixin, ListView):
    model = Order
    template_name = "html/order-list.html"
    context_object_name = "orders"

    def get_queryset(self):
        qs = super(OrderListView, self).get_queryset()
        return get_objects_for_user(self.request.user, ("view_order",), qs)


@login_required
def order_progress(request, order_pk):
    """
    Returns the current progress of the order in the following format:
    {
        “created”: 5,
        “total”: 20,
        “progress”: “25%”
    }
    """
    if not request.is_ajax():
        return HttpResponseBadRequest()

    order = get_object_or_404(Order, service_type=Order.SOCIAL_PROFILES,
                              pk=order_pk)
    if "view_order" not in get_perms(request.user, order):
        raise Http404
    return JsonResponse(order.order_progress)


class CouponCodeListView(ProtectedViewMixin,
                         ObjectPermissionRequiredMixin,
                         ListView):
    model = CouponCode
    perms = ("view_coupon_code",)
    template_name = "html/coupon-list.html"
    context_object_name = "coupons"

    def check_user(self, user):
        """
        Rep managers, staff users and sales reps is allowed to access this view
        """
        if not user.is_authenticated():
            return False

        check = user.is_staff or user.is_sales_rep or user.is_rep_manager
        return check


class AccountingReportView(ProtectedViewMixin, SingleTableView):
    table_class = PaymentTable
    model = Payment
    template_name = "accounting-report.html"
    paginate_by = 20

    def get_form(self):
        return AccountingReportForm(self.request.GET or None)

    def get_table_data(self):
        qs = super(AccountingReportView, self).get_table_data()
        return self.filter_queryset(qs)

    def filter_queryset(self, qs):
        form = self.get_form()
        date_range_q = status_q = Q()

        if form.is_valid():
            date_range = form.get_date_range()
            date_range_q = Q(modified__range=date_range) if date_range else Q()

            status = form.cleaned_data.get("status")
            status_q = Q(status=status) if status else Q()

        return qs.filter(date_range_q, status_q)

    def get_context_data(self, **kwargs):
        kwargs.update(form=self.get_form())
        return super(AccountingReportView, self).get_context_data(**kwargs)

    def check_user(self, user):
        return user.is_staff


accounting_report = AccountingReportView.as_view()
