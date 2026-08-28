*** Settings ***
Documentation     Catalog checks of the fictional web shop (neutral demo corpus).
...               Area tag `Catalog`, sub-area tags `Catalog-Search`,
...               `Catalog-Browse`.

*** Test Cases ***
Search Finds Product By Title
    [Tags]    Catalog    Catalog-Search    Smoke    Sprint-12    SHOP-1280
    Should Contain    ergonomic office chair    chair

Search Filters By Price Range
    [Tags]    Catalog    Catalog-Search    Regression    Sprint-13
    ${hits}=    Evaluate    [p for p in [19, 45, 90] if 20 <= p <= 60]
    Length Should Be    ${hits}    1

Empty Search Shows Suggestions
    [Tags]    Catalog    Catalog-Search    Regression    Sprint-14
    Should Not Be Empty    try: desk, chair, lamp

Search Handles Typos Eventually
    [Tags]    Catalog    Catalog-Search    Regression    Quarantine-Flaky
    [Documentation]    Quarantined: flaky in the fictional shop, excluded by
    ...    the usual NOT prefix:Quarantine- selections.
    Should Be True    ${True}

Sort By Price Ascending
    [Tags]    Catalog    Catalog-Browse    Regression    Sprint-12
    ${sorted}=    Evaluate    sorted([30, 10, 20])
    Should Be Equal    ${sorted}    ${{[10, 20, 30]}}

Category Page Paginates
    [Tags]    Catalog    Catalog-Browse    Regression    Sprint-13    SHOP-1291
    ${pages}=    Evaluate    -(-47 // 12)
    Should Be Equal As Integers    ${pages}    4

Out Of Stock Items Are Badged
    [Tags]    Catalog    Catalog-Browse    Smoke    Sprint-14
    Should Contain    out of stock    stock

Product Detail Shows The Stock Level
    [Tags]    Catalog    Catalog-Browse    Regression    Sprint-14
    ${left}=    Evaluate    12 - 5
    Should Be True    ${left} > 0
