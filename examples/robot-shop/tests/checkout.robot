*** Settings ***
Documentation     Checkout flows of the fictional web shop (neutral demo corpus).
...               Area tag `Checkout`, sub-area tags `Checkout-Guest`,
...               `Checkout-Registered`, `Checkout-Shipping`.

*** Variables ***
${STAGE}          dev

*** Test Cases ***
Guest Checkout Completes
    [Tags]    Checkout    Checkout-Guest    Smoke    Sprint-12    SHOP-1240
    Log    guest completes checkout
    Should Be True    ${True}

Guest Checkout Asks For An Email
    [Tags]    Checkout    Checkout-Guest    Regression    Sprint-13
    Should Contain    guest@example.invalid    @

Guest Cart Survives A Reload
    [Tags]    Checkout    Checkout-Guest    Regression    Sprint-13
    ${items}=    Evaluate    ["chair", "desk"]
    Length Should Be    ${items}    2

Stage Variable Reaches The Test
    [Tags]    Checkout    Checkout-Guest    VarCheck
    [Documentation]    Passes only when the run spec's runner variables
    ...    override the suite default (STAGE=dev → test).
    Should Be Equal    ${STAGE}    test

Registered Checkout Completes
    [Tags]    Checkout    Checkout-Registered    Regression    Sprint-12    SHOP-1241
    Log    registered user completes checkout
    Should Be True    ${True}

Saved Address Is Preselected
    [Tags]    Checkout    Checkout-Registered    Regression    Sprint-13
    Should Be Equal    12 Harbour Lane    12 Harbour Lane

Loyalty Points Are Applied
    [Tags]    Checkout    Checkout-Registered    Regression    Sprint-14    SHOP-1244
    ${total}=    Evaluate    120 - 12
    Should Be Equal As Integers    ${total}    108

Reorder Repeats The Last Basket
    [Tags]    Checkout    Checkout-Registered    Regression    Sprint-14
    ${basket}=    Evaluate    ["lamp", "lamp", "cable"]
    Length Should Be    ${basket}    3

Standard Shipping Is Free Above Threshold
    [Tags]    Checkout    Checkout-Shipping    Regression    Sprint-12
    ${fee}=    Evaluate    0 if 75 >= 50 else 5
    Should Be Equal As Integers    ${fee}    0

Express Shipping Shows A Date
    [Tags]    Checkout    Checkout-Shipping    Smoke    Sprint-13
    Should Match Regexp    2031-04-17    ^\\d{4}-\\d{2}-\\d{2}$

Pickup Point Can Be Chosen
    [Tags]    Checkout    Checkout-Shipping    Regression    Sprint-14    SHOP-1252
    Should Not Be Empty    Kiosk 12, Harbour Lane

Slow Warehouse Sync Finishes
    [Tags]    Checkout    Checkout-Shipping    Regression    SlowLane
    [Documentation]    Deliberately slow: keeps the run in RUNNING long enough
    ...    to observe live per-item status (DESIGN.md §6.2a listener path).
    Sleep    3s
    Log    warehouse sync finished

Slow Invoice Batch Finishes
    [Tags]    Checkout    Checkout-Shipping    Regression    SlowLane
    Sleep    6s
    Log    invoice batch finished
