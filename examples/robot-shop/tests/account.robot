*** Settings ***
Documentation     Customer account checks of the fictional web shop (neutral demo
...               corpus). Area tag `Account`, sub-area tags `Account-Profile`,
...               `Account-Orders`.

*** Test Cases ***
Profile Name Can Be Changed
    [Tags]    Account    Account-Profile    Regression    Sprint-12
    Should Be Equal    Robin Marsh    Robin Marsh

Newsletter Opt In Is Persisted
    [Tags]    Account    Account-Profile    Regression    Sprint-13    SHOP-1320
    Should Be True    ${True}

Address Book Holds Several Entries
    [Tags]    Account    Account-Profile    Smoke    Sprint-13
    ${addresses}=    Evaluate    ["home", "office"]
    Length Should Be    ${addresses}    2

Account Deletion Asks For Confirmation
    [Tags]    Account    Account-Profile    Regression    Sprint-14
    Should Contain    please confirm deletion    confirm

Order History Lists Past Orders
    [Tags]    Account    Account-Orders    Smoke    Sprint-12    SHOP-1330
    ${orders}=    Evaluate    ["A-1001", "A-1002", "A-1003"]
    Length Should Be    ${orders}    3

Invoice Pdf Can Be Downloaded
    [Tags]    Account    Account-Orders    Regression    Sprint-13
    Should Match Regexp    invoice-A-1002.pdf    \\.pdf$

Return Label Is Offered Within Fourteen Days
    [Tags]    Account    Account-Orders    Regression    Sprint-14    SHOP-1334
    ${days}=    Evaluate    9
    Should Be True    ${days} <= 14

Order Tracking Shows The Carrier
    [Tags]    Account    Account-Orders    Regression    Sprint-14
    Should Not Be Empty    Fleetfoot Express
