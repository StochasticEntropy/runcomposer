*** Settings ***
Documentation     Cart checks of the fictional web shop (neutral demo corpus).
...               Area tag `Cart`, sub-area tags `Cart-Basics`,
...               `Cart-Promotions`.

*** Test Cases ***
Add To Cart Increases The Count
    [Tags]    Cart    Cart-Basics    Smoke    Sprint-12    SHOP-1260
    ${count}=    Evaluate    2 + 1
    Should Be Equal As Integers    ${count}    3

Remove From Cart Decreases The Count
    [Tags]    Cart    Cart-Basics    Regression    Sprint-12
    ${count}=    Evaluate    3 - 1
    Should Be Equal As Integers    ${count}    2

Quantity Cannot Go Below One
    [Tags]    Cart    Cart-Basics    Regression    Sprint-13
    ${quantity}=    Evaluate    max(1, 0)
    Should Be Equal As Integers    ${quantity}    1

Cart Subtotal Sums Line Items
    [Tags]    Cart    Cart-Basics    Regression    Sprint-14    SHOP-1263
    ${subtotal}=    Evaluate    sum([19, 25, 6])
    Should Be Equal As Integers    ${subtotal}    50

Promo Code Reduces The Total
    [Tags]    Cart    Cart-Promotions    Smoke    Sprint-13    SHOP-1270
    ${total}=    Evaluate    100 - 10
    Should Be Equal As Integers    ${total}    90

Expired Promo Code Is Refused
    [Tags]    Cart    Cart-Promotions    Regression    Sprint-13
    Should Contain    promo code expired    expired

Two Promo Codes Do Not Stack
    [Tags]    Cart    Cart-Promotions    Regression    Sprint-14    SHOP-1274
    ${applied}=    Evaluate    ["WELCOME10", "SUMMER20"][:1]
    Length Should Be    ${applied}    1

Free Gift Threshold Is Honoured
    [Tags]    Cart    Cart-Promotions    Regression    Sprint-14
    ${gift}=    Evaluate    "mug" if 120 >= 100 else "none"
    Should Be Equal    ${gift}    mug
