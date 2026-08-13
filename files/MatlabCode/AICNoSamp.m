X=[ZsRatio,ZsGbar];
AIC = zeros(1,20);
GMModels = cell(1,20);
options = statset('MaxIter',500);
for k = 1:20
    GMModels{k} = fitgmdist(X,k,'CovarianceType','full','SharedCovariance',true,'Options',options,'Start','plus');
    AIC(k)= GMModels{k}.AIC;
end

[minAIC,numComponents] = min(AIC);
numComponents

BestModel = GMModels{numComponents}
[C,gmfit,p,NoOfinst,gmm] = ClusterCalc(X);