"""Centralised UI strings with English and Bengali support.

Every customer-facing string lives in the catalogue below. Handlers call
``t(key, lang, **params)`` rather than embedding literals, so adding a language
is a data change, not a code change. A missing Bengali string falls back to
English rather than showing a key.
"""

from __future__ import annotations

from typing import Any

from app.core.logging import get_logger
from app.domain.enums import Language

log = get_logger(__name__)

#: ``key -> {language: template}``. English is mandatory; Bengali is optional
#: and falls back to English.
CATALOG: dict[str, dict[str, str]] = {
    # -- generic ---------------------------------------------------------
    "btn.home": {"en": "🏠 Home", "bn": "🏠 হোম"},
    "btn.back": {"en": "◀ Back", "bn": "◀ পিছনে"},
    "btn.cancel": {"en": "❌ Cancel", "bn": "❌ বাতিল"},
    "btn.retry": {"en": "🔄 Retry", "bn": "🔄 আবার চেষ্টা"},
    "btn.support": {"en": "🎧 Support", "bn": "🎧 সাপোর্ট"},
    "btn.shop": {"en": "🛍 Shop", "bn": "🛍 শপ"},
    "btn.shop_now": {"en": "🛍 Shop Now", "bn": "🛍 এখনই কিনুন"},
    "btn.start_shopping": {"en": "🛍 Start Shopping", "bn": "🛍 কেনাকাটা শুরু করুন"},
    "btn.how_it_works": {"en": "📖 How It Works", "bn": "📖 কীভাবে কাজ করে"},
    "btn.my_orders": {"en": "📦 My Orders", "bn": "📦 আমার অর্ডার"},
    "btn.profile": {"en": "👤 Profile", "bn": "👤 প্রোফাইল"},
    "btn.referral": {"en": "🎁 Referral", "bn": "🎁 রেফারেল"},
    "btn.reseller": {"en": "🔗 Reseller", "bn": "🔗 রিসেলার"},
    "btn.notifications": {"en": "🔔 Notifications", "bn": "🔔 নোটিফিকেশন"},
    "btn.settings": {"en": "⚙️ Settings", "bn": "⚙️ সেটিংস"},
    "btn.details": {"en": "Details", "bn": "বিস্তারিত"},
    "btn.buy": {"en": "Buy", "bn": "কিনুন"},
    "btn.buy_now": {"en": "🛒 BUY NOW", "bn": "🛒 এখনই কিনুন"},
    "btn.search": {"en": "🔎 Search", "bn": "🔎 খুঁজুন"},
    "btn.copy": {"en": "📋 Copy", "bn": "📋 কপি"},
    "btn.done": {"en": "Done", "bn": "সম্পন্ন"},
    "btn.confirm_order": {"en": "✅ Confirm Order", "bn": "✅ অর্ডার নিশ্চিত করুন"},
    "btn.apply_coupon": {"en": "🎟 Apply Coupon", "bn": "🎟 কুপন প্রয়োগ"},
    "btn.enter_code": {"en": "✏️ Enter Code", "bn": "✏️ কোড লিখুন"},
    "btn.change": {"en": "✏️ Change", "bn": "✏️ পরিবর্তন"},
    "btn.try_again": {"en": "Try Again", "bn": "আবার চেষ্টা করুন"},
    "btn.continue_checkout": {"en": "Continue Checkout", "bn": "চেকআউট চালিয়ে যান"},
    "btn.i_paid": {"en": "✅ I've Paid", "bn": "✅ আমি পেমেন্ট করেছি"},
    "btn.submit_transaction": {"en": "🔗 Submit Transaction", "bn": "🔗 ট্রানজেকশন জমা দিন"},
    "btn.copy_address": {"en": "📋 Copy Address", "bn": "📋 ঠিকানা কপি"},
    "btn.qr_code": {"en": "📱 QR Code", "bn": "📱 QR কোড"},
    "btn.view_order": {"en": "📦 View Order", "bn": "📦 অর্ডার দেখুন"},
    "btn.order_details": {"en": "📦 Order Details", "bn": "📦 অর্ডার বিবরণ"},
    "btn.check_status": {"en": "🔄 Check Status", "bn": "🔄 স্ট্যাটাস দেখুন"},
    "btn.refresh": {"en": "🔄 Refresh", "bn": "🔄 রিফ্রেশ"},
    "btn.view_product": {"en": "🔐 View Product", "bn": "🔐 প্রোডাক্ট দেখুন"},
    "btn.receipt": {"en": "📄 Receipt", "bn": "📄 রসিদ"},
    "btn.continue_payment": {"en": "Continue Payment", "bn": "পেমেন্ট চালিয়ে যান"},
    "btn.new_payment": {"en": "🔄 Create New Payment", "bn": "🔄 নতুন পেমেন্ট তৈরি করুন"},
    "btn.notify_me": {"en": "🔔 Notify Me", "bn": "🔔 জানান"},
    "btn.reorder": {"en": "🔁 Reorder", "bn": "🔁 আবার অর্ডার"},
    "btn.delivery_status": {"en": "📦 View Delivery Status", "bn": "📦 ডেলিভারি স্ট্যাটাস"},
    "btn.contact_support": {"en": "🎧 Contact Support", "bn": "🎧 সাপোর্টে যোগাযোগ"},
    "btn.my_tickets": {"en": "🎫 My Tickets", "bn": "🎫 আমার টিকেট"},
    "btn.copy_link": {"en": "📋 Copy Link", "bn": "📋 লিঙ্ক কপি"},
    "btn.share": {"en": "📤 Share", "bn": "📤 শেয়ার"},
    "btn.referral_history": {"en": "📜 Referral History", "bn": "📜 রেফারেল ইতিহাস"},
    "btn.language": {"en": "🌐 Language", "bn": "🌐 ভাষা"},

    # -- welcome / home --------------------------------------------------
    "welcome.title": {"en": "👋 <b>Welcome</b>", "bn": "👋 <b>স্বাগতম</b>"},
    "welcome.body": {
        "en": "Welcome to our digital store.\n\nPremium products.\nSecure payments.\nFast delivery.",
        "bn": "আমাদের ডিজিটাল স্টোরে স্বাগতম।\n\nপ্রিমিয়াম প্রোডাক্ট।\nনিরাপদ পেমেন্ট।\nদ্রুত ডেলিভারি।",
    },
    "welcome.back_title": {"en": "👋 <b>Welcome back</b>", "bn": "👋 <b>আবার স্বাগতম</b>"},
    "home.title": {"en": "🏠 <b>HOME</b>", "bn": "🏠 <b>হোম</b>"},
    "home.greeting": {"en": "Hi {name}", "bn": "হ্যালো {name}"},
    "home.featured": {"en": "⭐ <b>Featured</b>", "bn": "⭐ <b>ফিচার্ড</b>"},
    "home.active_payment": {"en": "⚠️ <b>Active Payment</b>", "bn": "⚠️ <b>চলমান পেমেন্ট</b>"},
    "home.waiting_payment": {"en": "Waiting for payment", "bn": "পেমেন্টের অপেক্ষায়"},

    # -- shop ------------------------------------------------------------
    "shop.title": {"en": "🛍 <b>SHOP</b>", "bn": "🛍 <b>শপ</b>"},
    "shop.choose_category": {"en": "Choose a category.", "bn": "একটি ক্যাটাগরি বেছে নিন।"},
    "shop.best_sellers": {"en": "⭐ Best Sellers", "bn": "⭐ বেস্ট সেলার"},
    "shop.new_arrivals": {"en": "🆕 New Arrivals", "bn": "🆕 নতুন এসেছে"},
    "shop.search_prompt": {
        "en": "🔎 <b>SEARCH</b>\n\nSend a product name or keyword.",
        "bn": "🔎 <b>সার্চ</b>\n\nপ্রোডাক্টের নাম বা কীওয়ার্ড পাঠান।",
    },
    "shop.empty": {
        "en": "🛍 <b>SHOP</b>\n\nNo products are available right now.\nPlease check back soon.",
        "bn": "🛍 <b>শপ</b>\n\nএই মুহূর্তে কোনো প্রোডাক্ট নেই।\nশীঘ্রই আবার দেখুন।",
    },
    "shop.no_results": {
        "en": "🔎 No products matched your search.",
        "bn": "🔎 আপনার সার্চে কোনো প্রোডাক্ট মেলেনি।",
    },
    "product.in_stock": {"en": "🟢 In Stock", "bn": "🟢 স্টকে আছে"},
    "product.low_stock": {"en": "🟡 Only {count} left", "bn": "🟡 মাত্র {count}টি বাকি"},
    "product.out_of_stock": {"en": "🔴 Out of Stock", "bn": "🔴 স্টক শেষ"},
    "product.unavailable": {"en": "🔴 Currently unavailable", "bn": "🔴 এখন পাওয়া যাচ্ছে না"},
    "product.features": {"en": "✨ <b>Features</b>", "bn": "✨ <b>বৈশিষ্ট্য</b>"},
    "product.included": {"en": "📦 <b>What's included</b>", "bn": "📦 <b>যা যা আছে</b>"},
    "product.requirements": {"en": "📋 <b>Requirements</b>", "bn": "📋 <b>প্রয়োজনীয়তা</b>"},
    "product.faq": {"en": "❓ <b>FAQ</b>", "bn": "❓ <b>সাধারণ প্রশ্ন</b>"},
    "product.delivery_info": {"en": "🚚 <b>Delivery</b>", "bn": "🚚 <b>ডেলিভারি</b>"},

    # -- checkout --------------------------------------------------------
    "checkout.title": {"en": "🧾 <b>CHECKOUT</b>", "bn": "🧾 <b>চেকআউট</b>"},
    "checkout.confirm_title": {"en": "🧾 <b>CONFIRM ORDER</b>", "bn": "🧾 <b>অর্ডার নিশ্চিত করুন</b>"},
    "checkout.product": {"en": "Product", "bn": "প্রোডাক্ট"},
    "checkout.quantity": {"en": "Quantity", "bn": "পরিমাণ"},
    "checkout.price": {"en": "Price", "bn": "মূল্য"},
    "checkout.subtotal": {"en": "Subtotal", "bn": "সাবটোটাল"},
    "checkout.discount": {"en": "Discount", "bn": "ছাড়"},
    "checkout.total": {"en": "TOTAL", "bn": "মোট"},
    "coupon.title": {"en": "🎟 <b>APPLY COUPON</b>", "bn": "🎟 <b>কুপন প্রয়োগ</b>"},
    "coupon.prompt": {"en": "Enter your coupon code.", "bn": "আপনার কুপন কোড লিখুন।"},
    "coupon.invalid_title": {"en": "❌ <b>Invalid Coupon</b>", "bn": "❌ <b>অবৈধ কুপন</b>"},
    "coupon.invalid_body": {
        "en": "The coupon is expired,\ninvalid, or unavailable\nfor this order.",
        "bn": "কুপনটি মেয়াদোত্তীর্ণ, অবৈধ,\nঅথবা এই অর্ডারের জন্য প্রযোজ্য নয়।",
    },
    "coupon.applied_title": {"en": "✅ <b>Coupon Applied</b>", "bn": "✅ <b>কুপন প্রয়োগ হয়েছে</b>"},
    "coupon.new_total": {"en": "New Total", "bn": "নতুন মোট"},
    "coupon.none": {"en": "🎟 No coupons available.", "bn": "🎟 কোনো কুপন নেই।"},

    # -- payment ---------------------------------------------------------
    "payment.method_title": {"en": "💳 <b>PAYMENT METHOD</b>", "bn": "💳 <b>পেমেন্ট পদ্ধতি</b>"},
    "payment.exchange": {"en": "EXCHANGE", "bn": "এক্সচেঞ্জ"},
    "payment.blockchain": {"en": "BLOCKCHAIN", "bn": "ব্লকচেইন"},
    "payment.none_available": {
        "en": "⚠️ No payment methods are available right now.\nPlease contact support.",
        "bn": "⚠️ এই মুহূর্তে কোনো পেমেন্ট পদ্ধতি নেই।\nসাপোর্টে যোগাযোগ করুন।",
    },
    "payment.order": {"en": "Order", "bn": "অর্ডার"},
    "payment.amount": {"en": "Amount", "bn": "পরিমাণ"},
    "payment.network": {"en": "Network", "bn": "নেটওয়ার্ক"},
    "payment.destination": {"en": "Destination", "bn": "গন্তব্য"},
    "payment.reference": {"en": "Reference", "bn": "রেফারেন্স"},
    "payment.receiving_address": {"en": "Receiving Address", "bn": "প্রাপ্তির ঠিকানা"},
    "payment.expires_in": {"en": "Expires in", "bn": "মেয়াদ শেষ হবে"},
    "payment.memo_required": {
        "en": "⚠️ You MUST include this memo/comment:",
        "bn": "⚠️ অবশ্যই এই মেমো/কমেন্ট দিতে হবে:",
    },
    "payment.only_send": {
        "en": "⚠️ Send ONLY {asset}\non {network}.",
        "bn": "⚠️ শুধুমাত্র {asset} পাঠান\n{network} নেটওয়ার্কে।",
    },
    "payment.submit_reference_title": {
        "en": "🔗 <b>SUBMIT PAYMENT REFERENCE</b>",
        "bn": "🔗 <b>পেমেন্ট রেফারেন্স জমা দিন</b>",
    },
    "payment.submit_reference_prompt": {
        "en": "Enter your transaction/reference ID.",
        "bn": "আপনার ট্রানজেকশন/রেফারেন্স আইডি লিখুন।",
    },
    "payment.submitted_title": {"en": "🔎 <b>PAYMENT SUBMITTED</b>", "bn": "🔎 <b>পেমেন্ট জমা হয়েছে</b>"},
    "payment.submitted_body": {
        "en": "We're checking your payment.",
        "bn": "আমরা আপনার পেমেন্ট যাচাই করছি।",
    },
    "payment.detecting": {"en": "⏳ Detecting payment...", "bn": "⏳ পেমেন্ট খোঁজা হচ্ছে..."},
    "payment.leave_screen": {
        "en": "You can leave this screen.\nVerification will continue automatically.",
        "bn": "আপনি এই স্ক্রিন ছেড়ে যেতে পারেন।\nযাচাই স্বয়ংক্রিয়ভাবে চলতে থাকবে।",
    },
    "payment.verifying_title": {"en": "🔍 <b>VERIFYING PAYMENT</b>", "bn": "🔍 <b>পেমেন্ট যাচাই হচ্ছে</b>"},
    "payment.check_found": {"en": "✓ Transaction found", "bn": "✓ ট্রানজেকশন পাওয়া গেছে"},
    "payment.check_asset": {"en": "✓ Asset matched", "bn": "✓ অ্যাসেট মিলেছে"},
    "payment.check_network": {"en": "✓ Network matched", "bn": "✓ নেটওয়ার্ক মিলেছে"},
    "payment.check_receiver": {"en": "✓ Receiver matched", "bn": "✓ প্রাপক মিলেছে"},
    "payment.check_amount": {"en": "✓ Amount matched", "bn": "✓ পরিমাণ মিলেছে"},
    "payment.final_verification": {"en": "⏳ Final verification...", "bn": "⏳ চূড়ান্ত যাচাই..."},
    "payment.detected_title": {"en": "💰 <b>PAYMENT DETECTED</b>", "bn": "💰 <b>পেমেন্ট শনাক্ত হয়েছে</b>"},
    "payment.detected_body": {
        "en": "Your payment has been found.",
        "bn": "আপনার পেমেন্ট পাওয়া গেছে।",
    },
    "payment.awaiting_confirmation": {
        "en": "⏳ Waiting for final confirmation",
        "bn": "⏳ চূড়ান্ত নিশ্চিতকরণের অপেক্ষায়",
    },
    "payment.confirmations_title": {
        "en": "⏳ <b>WAITING FOR CONFIRMATION</b>",
        "bn": "⏳ <b>নিশ্চিতকরণের অপেক্ষায়</b>",
    },
    "payment.confirmations": {"en": "Confirmations", "bn": "নিশ্চিতকরণ"},
    "payment.confirmations_body": {
        "en": "Your transaction has been detected.\nWe're waiting for the required\nnetwork confirmation.",
        "bn": "আপনার ট্রানজেকশন শনাক্ত হয়েছে।\nআমরা নেটওয়ার্ক নিশ্চিতকরণের\nঅপেক্ষায় আছি।",
    },
    "payment.verified_title": {"en": "✅ <b>PAYMENT VERIFIED</b>", "bn": "✅ <b>পেমেন্ট যাচাই হয়েছে</b>"},
    "payment.verified_body": {"en": "Preparing your order...", "bn": "আপনার অর্ডার প্রস্তুত হচ্ছে..."},
    "payment.failed_title": {
        "en": "❌ <b>PAYMENT COULD NOT BE VERIFIED</b>",
        "bn": "❌ <b>পেমেন্ট যাচাই করা যায়নি</b>",
    },
    "payment.failed_body": {
        "en": (
            "We couldn't verify this payment.\n\n"
            "Possible reasons:\n"
            "• Transaction not found\n"
            "• Wrong network\n"
            "• Incorrect amount\n"
            "• Transaction failed\n\n"
            "Your order has NOT been marked as paid."
        ),
        "bn": (
            "আমরা এই পেমেন্ট যাচাই করতে পারিনি।\n\n"
            "সম্ভাব্য কারণ:\n"
            "• ট্রানজেকশন পাওয়া যায়নি\n"
            "• ভুল নেটওয়ার্ক\n"
            "• ভুল পরিমাণ\n"
            "• ট্রানজেকশন ব্যর্থ\n\n"
            "আপনার অর্ডার পরিশোধিত হিসেবে চিহ্নিত হয়নি।"
        ),
    },
    "payment.expired_title": {"en": "⚠️ <b>PAYMENT EXPIRED</b>", "bn": "⚠️ <b>পেমেন্টের মেয়াদ শেষ</b>"},
    "payment.expired_body": {
        "en": "The payment window has expired.\n\nThe order was NOT automatically\nmarked as paid.",
        "bn": "পেমেন্টের সময় শেষ হয়ে গেছে।\n\nঅর্ডারটি স্বয়ংক্রিয়ভাবে পরিশোধিত\nহিসেবে চিহ্নিত হয়নি।",
    },
    "payment.underpaid_title": {
        "en": "⚠️ <b>PAYMENT AMOUNT MISMATCH</b>",
        "bn": "⚠️ <b>পেমেন্টের পরিমাণ মেলেনি</b>",
    },
    "payment.expected": {"en": "Expected", "bn": "প্রত্যাশিত"},
    "payment.received": {"en": "Received", "bn": "প্রাপ্ত"},
    "payment.short": {"en": "Short", "bn": "ঘাটতি"},
    "payment.underpaid_body": {
        "en": "The order cannot be completed\nautomatically.",
        "bn": "অর্ডারটি স্বয়ংক্রিয়ভাবে সম্পন্ন\nকরা যাচ্ছে না।",
    },
    "payment.overpaid_title": {
        "en": "⚠️ <b>PAYMENT REVIEW REQUIRED</b>",
        "bn": "⚠️ <b>পেমেন্ট পর্যালোচনা প্রয়োজন</b>",
    },
    "payment.overpaid_body": {
        "en": "The payment requires review.",
        "bn": "পেমেন্টটি পর্যালোচনা প্রয়োজন।",
    },
    "payment.wrong_network_title": {"en": "⚠️ <b>WRONG NETWORK</b>", "bn": "⚠️ <b>ভুল নেটওয়ার্ক</b>"},
    "payment.detected_label": {"en": "Detected", "bn": "শনাক্ত"},
    "payment.wrong_network_body": {
        "en": "The payment has NOT been\nautomatically credited.",
        "bn": "পেমেন্ট স্বয়ংক্রিয়ভাবে জমা হয়নি।",
    },
    "payment.wrong_asset_title": {"en": "⚠️ <b>WRONG ASSET</b>", "bn": "⚠️ <b>ভুল অ্যাসেট</b>"},
    "payment.wrong_asset_body": {
        "en": "The transaction does not contain\nthe expected asset.\n\nThe payment has NOT been\nautomatically credited.",
        "bn": "ট্রানজেকশনে প্রত্যাশিত অ্যাসেট নেই।\n\nপেমেন্ট স্বয়ংক্রিয়ভাবে জমা হয়নি।",
    },
    "payment.duplicate_title": {
        "en": "⚠️ <b>TRANSACTION ALREADY USED</b>",
        "bn": "⚠️ <b>ট্রানজেকশন ইতিমধ্যে ব্যবহৃত</b>",
    },
    "payment.duplicate_body": {
        "en": "This transaction has already been\nassociated with another order.\n\nFor security, it cannot be reused.",
        "bn": "এই ট্রানজেকশন ইতিমধ্যে অন্য অর্ডারে\nব্যবহৃত হয়েছে।\n\nনিরাপত্তার কারণে এটি পুনরায় ব্যবহার করা যাবে না।",
    },
    "payment.review_title": {"en": "🔎 <b>PAYMENT UNDER REVIEW</b>", "bn": "🔎 <b>পেমেন্ট পর্যালোচনাধীন</b>"},
    "payment.review_body": {
        "en": (
            "Your payment requires additional\nverification.\n\n"
            "No action is required right now.\n\n"
            "We will update your order when\nthe review is complete."
        ),
        "bn": (
            "আপনার পেমেন্টের অতিরিক্ত যাচাই প্রয়োজন।\n\n"
            "এখন কোনো পদক্ষেপ নেওয়ার দরকার নেই।\n\n"
            "পর্যালোচনা শেষ হলে আমরা আপনার অর্ডার আপডেট করব।"
        ),
    },

    # -- delivery --------------------------------------------------------
    "delivery.preparing_title": {"en": "📦 <b>PREPARING YOUR ORDER</b>", "bn": "📦 <b>আপনার অর্ডার প্রস্তুত হচ্ছে</b>"},
    "delivery.payment_confirmed": {"en": "Payment confirmed ✓", "bn": "পেমেন্ট নিশ্চিত ✓"},
    "delivery.inventory_allocated": {"en": "Inventory:\nAllocated ✓", "bn": "ইনভেন্টরি:\nবরাদ্দ ✓"},
    "delivery.preparing": {"en": "Delivery:\nPreparing...", "bn": "ডেলিভারি:\nপ্রস্তুত হচ্ছে..."},
    "delivery.ready_title": {"en": "📦 <b>YOUR ORDER IS READY</b>", "bn": "📦 <b>আপনার অর্ডার প্রস্তুত</b>"},
    "delivery.completed": {"en": "Delivery:\nCompleted ✓", "bn": "ডেলিভারি:\nসম্পন্ন ✓"},
    "delivery.delayed_title": {"en": "⚠️ <b>DELIVERY DELAYED</b>", "bn": "⚠️ <b>ডেলিভারি বিলম্বিত</b>"},
    "delivery.delayed_body": {
        "en": (
            "Your payment is confirmed.\n\n"
            "However, product delivery encountered\na temporary issue.\n\n"
            "Your payment is safe.\n\n"
            "We are retrying automatically."
        ),
        "bn": (
            "আপনার পেমেন্ট নিশ্চিত হয়েছে।\n\n"
            "তবে প্রোডাক্ট ডেলিভারিতে সাময়িক সমস্যা হয়েছে।\n\n"
            "আপনার পেমেন্ট নিরাপদ আছে।\n\n"
            "আমরা স্বয়ংক্রিয়ভাবে আবার চেষ্টা করছি।"
        ),
    },
    "product.your_product": {"en": "🔐 <b>YOUR PRODUCT</b>", "bn": "🔐 <b>আপনার প্রোডাক্ট</b>"},
    "product.keep_secure": {
        "en": "⚠️ Keep this information secure.",
        "bn": "⚠️ এই তথ্য নিরাপদে রাখুন।",
    },
    "product.manual_delivery": {
        "en": "Our team will complete this order shortly.",
        "bn": "আমাদের টিম শীঘ্রই এই অর্ডার সম্পন্ন করবে।",
    },

    # -- orders ----------------------------------------------------------
    "orders.title": {"en": "📦 <b>MY ORDERS</b>", "bn": "📦 <b>আমার অর্ডার</b>"},
    "orders.filter_all": {"en": "All", "bn": "সব"},
    "orders.filter_pending": {"en": "Pending", "bn": "অপেক্ষমাণ"},
    "orders.filter_paid": {"en": "Paid", "bn": "পরিশোধিত"},
    "orders.filter_completed": {"en": "Completed", "bn": "সম্পন্ন"},
    "orders.filter_cancelled": {"en": "Cancelled", "bn": "বাতিল"},
    "orders.empty": {
        "en": "📦 No orders yet.\n\nStart shopping to create your\nfirst order.",
        "bn": "📦 এখনো কোনো অর্ডার নেই।\n\nপ্রথম অর্ডার করতে কেনাকাটা শুরু করুন।",
    },
    "order.payment": {"en": "Payment", "bn": "পেমেন্ট"},
    "order.delivery": {"en": "Delivery", "bn": "ডেলিভারি"},
    "order.created": {"en": "Created", "bn": "তৈরি"},
    "order.verified": {"en": "✅ Verified", "bn": "✅ যাচাইকৃত"},
    "order.pending": {"en": "⏳ Pending", "bn": "⏳ অপেক্ষমাণ"},
    "order.complete": {"en": "✅ Completed", "bn": "✅ সম্পন্ন"},

    # -- profile / referral / notifications ------------------------------
    "profile.title": {"en": "👤 <b>PROFILE</b>", "bn": "👤 <b>প্রোফাইল</b>"},
    "profile.account": {"en": "Account", "bn": "অ্যাকাউন্ট"},
    "profile.orders": {"en": "Orders", "bn": "অর্ডার"},
    "profile.completed": {"en": "Completed", "bn": "সম্পন্ন"},
    "profile.total_spent": {"en": "Total Spent", "bn": "মোট ব্যয়"},
    "referral.title": {"en": "🎁 <b>REFERRAL PROGRAM</b>", "bn": "🎁 <b>রেফারেল প্রোগ্রাম</b>"},
    "referral.body": {
        "en": "Invite friends and earn rewards.",
        "bn": "বন্ধুদের আমন্ত্রণ জানান এবং পুরস্কার জিতুন।",
    },
    "referral.your_link": {"en": "Your link:", "bn": "আপনার লিঙ্ক:"},
    "referral.invited": {"en": "Invited", "bn": "আমন্ত্রিত"},
    "referral.qualified": {"en": "Qualified", "bn": "যোগ্য"},
    "referral.earned": {"en": "Earned", "bn": "অর্জিত"},
    "referral.empty": {"en": "🎁 No referrals yet.", "bn": "🎁 এখনো কোনো রেফারেল নেই।"},
    "notifications.title": {"en": "🔔 <b>NOTIFICATIONS</b>", "bn": "🔔 <b>নোটিফিকেশন</b>"},
    "notifications.empty": {"en": "🔔 No notifications.", "bn": "🔔 কোনো নোটিফিকেশন নেই।"},

    # -- support ---------------------------------------------------------
    "support.title": {"en": "🎧 <b>SUPPORT</b>", "bn": "🎧 <b>সাপোর্ট</b>"},
    "support.how_can_we_help": {"en": "How can we help?", "bn": "আমরা কীভাবে সাহায্য করতে পারি?"},
    "support.cat_payment": {"en": "💳 Payment Issue", "bn": "💳 পেমেন্ট সমস্যা"},
    "support.cat_order": {"en": "📦 Order Issue", "bn": "📦 অর্ডার সমস্যা"},
    "support.cat_product": {"en": "🔑 Product Issue", "bn": "🔑 প্রোডাক্ট সমস্যা"},
    "support.cat_technical": {"en": "🔧 Technical Issue", "bn": "🔧 কারিগরি সমস্যা"},
    "support.cat_other": {"en": "💬 Other", "bn": "💬 অন্যান্য"},
    "support.describe": {
        "en": "Please describe your issue in one message.",
        "bn": "একটি বার্তায় আপনার সমস্যা বর্ণনা করুন।",
    },
    "support.created": {"en": "✅ <b>Ticket Created</b>", "bn": "✅ <b>টিকেট তৈরি হয়েছে</b>"},
    "support.ticket": {"en": "Ticket", "bn": "টিকেট"},
    "support.empty": {"en": "🎧 No support tickets.", "bn": "🎧 কোনো সাপোর্ট টিকেট নেই।"},
    "support.reply_prompt": {"en": "Send your reply.", "bn": "আপনার উত্তর পাঠান।"},

    # -- reseller --------------------------------------------------------
    "reseller.center_title": {"en": "🔗 <b>RESELLER CENTER</b>", "bn": "🔗 <b>রিসেলার সেন্টার</b>"},
    "reseller.center_body": {
        "en": "Sell our products through\nyour own bot, website, or app.",
        "bn": "আপনার নিজের বট, ওয়েবসাইট বা অ্যাপের\nমাধ্যমে আমাদের প্রোডাক্ট বিক্রি করুন।",
    },
    "reseller.become": {"en": "🚀 Become a Reseller", "bn": "🚀 রিসেলার হোন"},
    "reseller.api_docs": {"en": "📚 API Documentation", "bn": "📚 API ডকুমেন্টেশন"},
    "reseller.api_keys": {"en": "🔑 API Keys", "bn": "🔑 API কী"},
    "reseller.dashboard": {"en": "📊 Dashboard", "bn": "📊 ড্যাশবোর্ড"},
    "reseller.activate": {"en": "✅ Activate Reseller Account", "bn": "✅ রিসেলার অ্যাকাউন্ট চালু করুন"},
    "reseller.learn_more": {"en": "📖 Learn More", "bn": "📖 আরও জানুন"},
    "reseller.status": {"en": "Status", "bn": "স্ট্যাটাস"},
    "reseller.api_requests": {"en": "API Requests", "bn": "API রিকোয়েস্ট"},
    "reseller.sales": {"en": "Sales", "bn": "বিক্রয়"},
    "reseller.webhooks": {"en": "Webhooks", "bn": "ওয়েবহুক"},
    "reseller.key_created": {"en": "✅ <b>API KEY CREATED</b>", "bn": "✅ <b>API কী তৈরি হয়েছে</b>"},
    "reseller.store_securely": {
        "en": "⚠️ Store this securely.\nIt will not be shown again.",
        "bn": "⚠️ এটি নিরাপদে সংরক্ষণ করুন।\nএটি আর দেখানো হবে না।",
    },
    "reseller.no_keys": {"en": "🔑 No API keys yet.", "bn": "🔑 এখনো কোনো API কী নেই।"},

    # -- errors / states -------------------------------------------------
    "error.generic": {
        "en": "⚠️ Something went wrong.\n\nPlease try again.",
        "bn": "⚠️ কিছু একটা সমস্যা হয়েছে।\n\nআবার চেষ্টা করুন।",
    },
    "error.not_found": {
        "en": "⚠️ We couldn't find that.",
        "bn": "⚠️ আমরা সেটি খুঁজে পাইনি।",
    },
    "error.expired_session": {
        "en": "⚠️ This screen has expired.\n\nPlease start again.",
        "bn": "⚠️ এই স্ক্রিনের মেয়াদ শেষ।\n\nআবার শুরু করুন।",
    },
    "error.rate_limited": {
        "en": "⏳ You're going too fast.\nPlease wait a moment.",
        "bn": "⏳ আপনি খুব দ্রুত করছেন।\nএকটু অপেক্ষা করুন।",
    },
    "error.banned": {
        "en": "🚫 Your account has been restricted.\nPlease contact support.",
        "bn": "🚫 আপনার অ্যাকাউন্ট সীমিত করা হয়েছে।\nসাপোর্টে যোগাযোগ করুন।",
    },
    "maintenance.notice": {
        "en": "🔧 <b>Maintenance</b>\n\nWe're performing scheduled maintenance.\nPlease check back shortly.",
        "bn": "🔧 <b>রক্ষণাবেক্ষণ</b>\n\nআমরা নির্ধারিত রক্ষণাবেক্ষণ করছি।\nশীঘ্রই আবার দেখুন।",
    },
    "loading.default": {"en": "⏳ Loading...", "bn": "⏳ লোড হচ্ছে..."},
    "loading.checking_payment": {"en": "⏳ Checking payment...", "bn": "⏳ পেমেন্ট যাচাই হচ্ছে..."},
    "loading.creating_order": {"en": "⏳ Creating order...", "bn": "⏳ অর্ডার তৈরি হচ্ছে..."},
    "loading.loading_products": {"en": "⏳ Loading products...", "bn": "⏳ প্রোডাক্ট লোড হচ্ছে..."},
    "loading.preparing_delivery": {"en": "⏳ Preparing delivery...", "bn": "⏳ ডেলিভারি প্রস্তুত হচ্ছে..."},
    "success.order_created": {"en": "✅ Order Created", "bn": "✅ অর্ডার তৈরি হয়েছে"},
    "success.copied": {"en": "✅ Copied", "bn": "✅ কপি হয়েছে"},
    "success.saved": {"en": "✅ Saved", "bn": "✅ সংরক্ষিত"},
    "success.subscribed": {
        "en": "🔔 We'll notify you when it's back in stock.",
        "bn": "🔔 স্টকে এলে আমরা আপনাকে জানাব।",
    },
}


class Translator:
    """Resolves a key for a language, with English fallback."""

    def __init__(self, catalog: dict[str, dict[str, str]] | None = None) -> None:
        self.catalog = catalog if catalog is not None else CATALOG

    def get(self, key: str, language: Language | str = Language.EN, **params: Any) -> str:
        lang = language.value if isinstance(language, Language) else str(language)
        entry = self.catalog.get(key)
        if entry is None:
            log.warning("i18n.missing_key", key=key, language=lang)
            return key
        template = entry.get(lang) or entry.get(Language.EN.value) or key
        if not params:
            return template
        try:
            return template.format(**params)
        except (KeyError, IndexError) as exc:
            log.warning("i18n.format_failed", key=key, error=str(exc))
            return template

    def missing_keys(self, language: Language) -> list[str]:
        """Keys not yet translated for a language (used by a coverage test)."""
        return [key for key, entry in self.catalog.items() if language.value not in entry]


_translator = Translator()


def t(key: str, language: Language | str = Language.EN, **params: Any) -> str:
    return _translator.get(key, language, **params)


def get_translator() -> Translator:
    return _translator
