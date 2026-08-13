function [checkme] = CheckIfClustered(X,clustersGauss,T1tresh)
for i=1:length(clustersGauss)
for j=1:3
    fvalue=(clustersGauss(i).NoOfInst(j)*(clustersGauss(i).NoOfInst(j)-2)/(clustersGauss(i).NoOfInst(j)-1)*2)*(X-clustersGauss(i).mu(j))*(clustersGauss(i).Sigma(:,:,j)^-1)*(X-clustersGauss(i).mu(j))';
    if (fvalue <T1tresh)
        checkme = 1;
        X
    else
        checkme = 0;
    end
end
end
    
end