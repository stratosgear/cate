rem Don't forget to activate environment
set CATE_DISABLE_WEB_TESTS=1
set CATE_DISABLE_PLOT_TESTS=1
set CATE_DISABLE_GEOPANDAS_TESTS=1
py.test test
