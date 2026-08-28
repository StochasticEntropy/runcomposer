*** Settings ***
Documentation     Payment checks of the fictional web shop (neutral demo corpus).
...               Area tag `Payments`, sub-area tags `Payments-Cards`,
...               `Payments-Wallets`, `Payments-Refunds` — the same tag world the
...               bundled `runcomposer demo` corpus uses (DESIGN.md §12).

*** Test Cases ***
Visa Payment Succeeds
    [Tags]    Payments    Payments-Cards    Smoke    Sprint-12    SHOP-1200
    Log    charging a visa card
    Should Be Equal    ${True}    ${True}

Mastercard Payment Succeeds
    [Tags]    Payments    Payments-Cards    Regression    Sprint-13
    Log    charging a mastercard
    Should Be True    ${1} == ${1}

Declined Card Shows Error
    [Tags]    Payments    Payments-Cards    Regression    Sprint-14
    Log    declining a card on purpose
    Should Contain    payment declined    declined

Three D Secure Challenge Completes
    [Tags]    Payments    Payments-Cards    Regression    Sprint-12    SHOP-1203
    ${step}=    Evaluate    "challenge" if True else "frictionless"
    Should Be Equal    ${step}    challenge

Card Number Is Masked In Receipts
    [Tags]    Payments    Payments-Cards    Regression    Sprint-13
    ${masked}=    Set Variable    **** **** **** 4242
    Should Contain    ${masked}    4242

Expired Card Is Rejected Loudly
    [Tags]    Payments    Payments-Cards    Regression    Sprint-14    SHOP-1211
    [Documentation]    Deliberately red: the demo corpus ships one failing test.
    Fail    simulated defect: expired card was accepted

Wallet Payment Succeeds
    [Tags]    Payments    Payments-Wallets    Smoke    Sprint-13    SHOP-1220
    Log    paying with a stored wallet
    Should Be True    ${True}

Wallet Top Up Is Booked
    [Tags]    Payments    Payments-Wallets    Regression    Sprint-13
    ${balance}=    Evaluate    40 + 60
    Should Be Equal As Integers    ${balance}    100

Wallet Currency Conversion Rounds Half Up
    [Tags]    Payments    Payments-Wallets    Regression    Sprint-14
    ${amount}=    Evaluate    round(19.994, 2)
    Should Be Equal As Numbers    ${amount}    19.99

Wallet Timeout Falls Back To Card
    [Tags]    Payments    Payments-Wallets    Regression    Quarantine-Flaky
    [Documentation]    Quarantined: timing-dependent in the fictional shop,
    ...    excluded by the usual NOT prefix:Quarantine- selections.
    Should Be True    ${True}

Refund Restores Balance
    [Tags]    Payments    Payments-Refunds    Regression    Sprint-12    SHOP-1231
    ${balance}=    Evaluate    100 - 25 + 25
    Should Be Equal As Integers    ${balance}    100

Partial Refund Keeps Remainder
    [Tags]    Payments    Payments-Refunds    Regression    Sprint-14
    ${remainder}=    Evaluate    80 - 30
    Should Be Equal As Integers    ${remainder}    50

Refund Of A Refund Is Refused
    [Tags]    Payments    Payments-Refunds    Regression    Sprint-14    SHOP-1232
    Should Contain    order already refunded    already refunded
